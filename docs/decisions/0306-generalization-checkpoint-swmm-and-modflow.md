# ADR 0306 - The generalization checkpoint: one SWMM and one MODFLOW template

Status: LANDED (wave 4 of the declarative campaign - the checkpoint)
Design: `docs/design/declarative-workflows.md` (migration-order item 4)
Builds on: ADR 0303 (library v1 + do_sag), ADR 0304 (form + draw cards),
ADR 0305 (river_dye + the live-run harness)

## Context

Three waves of the declarative campaign ran entirely on TELEMAC. The library
could be TELEMAC-shaped without anyone noticing: one engine, one deck writer, one
solve lane, one product type. The design doc therefore inserts a CHECKPOINT
before any mass conversion - migrate ONE template on each of the two priority
engines and answer, in writing, what the library LACKED for each engine's shape.

This is that wave. Nothing about the campaign's schedule changes if the answer is
"very little"; the point is that the answer is now evidence rather than a hope.

## THE CHECKPOINT QUESTION, ANSWERED FIRST

### What the library lacked - MODFLOW

1. **It could not hand back the sheet the run actually ran on.** `RunResult`
   carried the terminal value, the results, the provenance rows and the charts -
   but not the `ResolvedParams` a form gate had revised. Any caller that narrates
   from the sheet it passed IN reports the values the user REPLACED, while the
   solver used the approved ones. river_dye never noticed because it narrates
   only from its returned layer; the MODFLOW template narrates a `summary` and
   writes a `metrics.json`, so it hit this immediately - and the FIRST live card
   drive recorded `porosity = 0.157` in the run's own metrics while mf6 had
   solved on the approved `0.25`. **Fixed** (`RunResult.params`, 6 lines).

2. **`Param.real_source` was unconditional.** A param that declares where its
   value comes from claimed that source even when the caller TYPED the value, so
   a user-supplied aquifer K shipped a provenance row crediting SoilGrids.
   **Fixed** in `resolver._finish`: a real source survives only on a
   `derived`/`fetched` basis.

3. **A derivation had no way to record the evidence it read.** do_sag's
   derivations are pure arithmetic, so `note="derived by <path>"` was enough.
   A derivation that READS THE WORLD knows which texture it sampled and whether
   the fit clamped, and that belongs on the row the form card renders - not in a
   log line. **Fixed**: a derivation may return `Derived(value, note,
   real_source)` and the resolver seats it (a bare value still works).

4. **The provenance row rounded floats to four DECIMAL places.** A hydraulic
   conductivity of `9.3e-07` was reported as `0.0`. **Fixed**: six significant
   figures.

5. **NOT a gap, but the shape is different and worth stating.** MODFLOW's deck
   never appears in the workflow layer at all: the archetype name selects a deck
   writer inside `run_modflow`, so the template's whole engine surface is
   `MODFLOWRunArgs`. There is nothing for a `WriteDeck.modflow` hook to serialize
   - the template-method hook for this engine is the RUN ARGS, and that is what
   `RunArchetype.<archetype>` is. The package model stayed where it was.

6. **A structural mismatch recorded, not fixed.** `MODFLOWRunArgs` is a
   plume-shaped envelope, so every GWF-only archetype must fill
   `contaminant` / `release_rate_kg_s` / `duration_days` with inert values its
   deck writer never reads. At HEAD those literals sat in each composer; they are
   now in ONE place (`steps/archetype._INERT_TRANSPORT`) with the constraint
   named. Fixing it properly means splitting the args model - a contracts change,
   out of scope for a checkpoint.

### What the library lacked - SWMM

7. **Nothing structural.** The plan is five declared steps (deck, solve, deck,
   solve, metrics), the deck writer is the per-question hook the template-method
   pattern predicted, and the headless pyswmm run loop is a genuinely shared step
   the whole family can declare. The interpreter did not fight the host-exec lane
   at all: a step is a step whether it dispatches a container or steps a
   simulation in-process.

8. **The harness could not cite a chart-first run's product.** `RunEvidence`
   locates the run prefix from a published raster and reads `metrics.json` back
   off it. This template publishes NO raster - it is the chart-first validation
   class, and a schematic deck has no run prefix at all - so `require_run_products`
   is structurally unavailable. Its product IS the chart spec, which crosses the
   wire. **Fixed** (minimal): the harness keeps the chart PAYLOADS, not just a
   count, and `require_chart(title_contains=...)` asserts on one.

9. **The harness located the run prefix from the WRONG raster.** It took the
   first `s3://` raster on the canvas. A run that surfaces its fetched inputs
   through the emit-on-fetch seam puts CONTEXT rasters (here, two SoilGrids
   inputs, in the cache bucket) ahead of its result, so the harness read
   `run_id = "cache"` and reported the run's own products missing. **Fixed**:
   prefer `role="primary"`.

10. **Law 9 fired the moment the deck's literals became declarations - and it
    was right to.** HEAD's `build_aquifer_inp` carried six physics-shaped numbers
    inside an f-string: the subcatchment imperviousness, the three Green-Ampt
    infiltration parameters, the deep-seepage rate and the pan evaporation.
    Invisible to law 9, invisible to the user, uneditable. Declaring them made
    the auto-mode floor refuse the whole template. That is the checkpoint
    earning its keep: the refusal is a true statement about HEAD. See the fork
    below for how they are tagged and what is queued.

### The one gap both engines hit, which is therefore NOT a TELEMAC quirk

11. **A `Data` producer may Ref a param or another `Data`, never a step** - so
    any fetch that depends on a resolved LOCATION cannot be declared as `Data`.
    ADR 0305 recorded this for river_dye's carrier discharge and offered NATE a
    fork (move the gate after the producers, giving up "gates before steps").
    Both engines hit it here, and both hit it on their PRIMARY real-data input.

    **This wave answers the fork instead of re-asking it, and the answer needs no
    library change.** The escape hatch is the DERIVED DOOR. Derivations run
    inside `resolve_params`, BEFORE the plan is built and therefore before the
    form gate - and they may be `async`, so a derivation can geocode and can
    fetch. Declaring the AOI point and the aquifer properties as DERIVED params
    instead of resolving them inside a step means the form card shows
    `aquifer_k_ms = 9.298e-07 m/s` with the badge `derived by aquifer_k_ms` and
    the note naming the texture it was fitted from - the real number, before
    approval, which is exactly what ADR 0305's delta 1 said had been LOST.

    The rule this checkpoint proposes: **an artifact is `Data`; a point SAMPLE is
    a `Param` through the DERIVED door.** `Data` reads the Domain environment
    (which carries an extent), so it fits rasters and layers; a value sampled at
    a point fits a form cell, wants bounds, wants to be editable, and wants to be
    on the card. Both templates here declare `DATA = ()` for that reason, and
    neither is poorer for it.

    river_dye's carrier discharge is the same shape and could adopt this. That
    is a river_dye change, not a checkpoint change, and it is queued rather than
    taken.

**Verdict: the library is not TELEMAC-shaped.** Four small additions (60 lines
across the library and the harness), no architectural gap, and one open fork
(#10) that is a physics question rather than a library question.

## Template selection, reasoned

The kickoff's rule: the SMALLEST template per engine that still exercises a real
solve, at least one derivation seam, and real results.

### MODFLOW: `regional_water_budget` (360 lines), not `capture_zone` (1,457)

The kickoff named `capture_zone` as the strong candidate, for its SoilGrids-derived
K seam - the law-9 derivation precedent. Verified against the code (law 5): **that
seam is not capture_zone's.** It lives in `workflows/shared/aquifer_resolve.py`
behind `workflows/modflow/_input_review.resolve_and_gate_aquifer`, and TEN MODFLOW
templates call it identically. `regional_water_budget` exercises the same
derivation, the same `run_modflow_archetype_job` dispatch, the same typed
`LayerURI` result and the same chart seam - in a quarter of the lines, and
without capture_zone's second engine leg (native mf6 PRT particle tracking) or
its well-geometry surface.

`capture_zone` is also the LARGEST template in the MODFLOW family, larger than
`swmm_urban_flood`'s sibling class. Under the kickoff's own "avoid the giants -
they are campaign work" rule it disqualifies itself. Ranked by size, the smaller
candidates fail the criteria: `wellhead_protection` (166) is a thin delegate INTO
capture_zone, so migrating it means migrating capture_zone; `package_validation`
(257) runs synthetic benchmark decks with no fetch, no derivation and no
georeferenced product.

### SWMM: `swmm_aquifer_baseflow_to_node` (560 lines)

Inventoried all 15 registered SWMM templates. Only four make a REAL data fetch:

| template | lines | real fetch | real solve | product |
|---|---|---|---|---|
| `aquifer_baseflow` | 560 | geocode + SoilGrids | pyswmm x2 | chart + scalars |
| `dual_drainage` | 457 | 3DEP + OSM + Atlas-14 | pyswmm | raster + scalars |
| `network_import` | 658 | ArcGIS/GeoJSON + 3DEP + Atlas-14 | pyswmm | raster |
| `urban_flood` | 1,610 | 3DEP + OSM + Atlas-14 | pyswmm | raster |

The other eleven are thin composers over SYNTHETIC mechanism stubs (the five
`*_comparison` templates, the three `deck_*` templates) with schematic
coordinates, no fetch and no derivation - and the mechanism-template purity fork
is explicitly deferred out of v1. `dual_drainage` is the only smaller candidate
with a fetch, and it is built ON `urban_flood`'s and `network_import`'s
machinery, so migrating it drags both giants in. `aquifer_baseflow` is the
smallest template that fetches, derives and solves without pulling a giant
behind it - the class the kickoff named.

The cost of the pick, stated: it publishes no map layer, so the LAYER half of the
acceptance is carried by MODFLOW. That turned out to be the more informative
choice, because it is how the harness gap (#8) was found.

## Inventory - MODFLOW `modflow_regional_water_budget`

Every variable and behavior at HEAD, and where it went.

| # | old behavior (`regional_water_budget.py` @ HEAD) | re-homing |
|---|---|---|
| 1 | `_coerce_optional_latlon(aoi_latlon)` | `_normalize`, via `coerce_latlon`; a bad point now refuses `REGIONAL_WATER_BUDGET_INPUT_INVALID` instead of raising |
| 2 | "exactly one of location or aoi_latlon", raised inside `_resolve_aoi_point` | `_normalize` refuses EARLIER (before any door) when neither is given. CHANGED: supplying BOTH is no longer an error - the explicit point wins at door 1 and the place name still names the run. See delta 1 |
| 3 | `_resolve_aoi_point` - geocode, `substep`, `_maybe_emit`, typed no-centroid error | Param `aoi_latlon`, door=DERIVED, `steps.aoi.aoi_latlon`; the geocode is memoized in `workflows/shared/site_resolve` |
| 4 | `location_name` threaded through `derived`/`summary` | Param `location_name`, door=DERIVED, `steps.aoi.location_name` |
| 5 | `resolve_and_gate_aquifer(...)` - SoilGrids K + porosity AND an input-review gate | SPLIT. The resolution is Params `aquifer_k_ms` / `porosity`, door=DERIVED, `steps.aquifer.*`, each `user_lever`, bounded, carrying `Derived` evidence. The gate is the plan's `FormGate` |
| 6 | `AquiferRefusal` -> `RegionalWaterBudgetScenarioError` | `ModflowPhysicsInputRequired`, `error_code="PHYSICS_INPUT_REQUIRED"` - the same code the library's own law-9 refusal uses, so callers route on the reason |
| 7 | `MODFLOWRunArgs(...)` assembly + `RegionalWaterBudgetInputError` on a bad arg set | `steps.archetype.run_archetype`; `MODFLOW_ARCHETYPE_ARGS_INVALID` |
| 8 | `contaminant="n/a"`, `release_rate_kg_s=1.0`, `duration_days=1.0` | `steps/archetype._INERT_TRANSPORT`, with the constraint named. See checkpoint answer #6 |
| 9 | `_run_archetype` (imported from `sustainable_yield`) - substep, `_maybe_emit`, typed-layer check, `add_loaded_layer` | `steps.archetype.run_archetype`. The `substep` and `_maybe_emit` wrappers are DELETED: the interpreter owns substep accounting (law 8), and `pipeline_emitter` was always `None` from the thin tool, so `_maybe_emit` always ran direct |
| 10 | `begin_substeps(current_emitter(), _planned)` with a hand-counted `_planned` | DELETED. The interpreter counts its own nodes |
| 11 | `build_budget_partition_chart` + `emit_chart_payloads`, called inline | `.chart("budget_partition", builder=steps.products.build_budget_chart)`. The SPEC is now a product: it rides out on `RunResult.charts` and is persisted |
| 12 | `gate_and_stamp_modflow_inputs(...)` AFTER the solve | The STAMP survives as `merge_provenance` in the tool body. The GATE is DELETED: it sat after the consequential step, so nothing it could have changed was still ahead of it - the exact "dead gate" the plan validator refuses. HEAD gated the same values TWICE, once usefully and once not |
| 13 | `provenance_summary(resolution)` + the planning-level caveat prose | `_aquifer_provenance(ran)`, read off the run's OWN rows so the prose cannot drift from the record |
| 14 | `zone_partition`, unvalidated, passed straight to the deck writer | Param `zone_partition`, door=USER, optional, with an allow-list in `_normalize`. CHANGED: an unknown scheme now falls back to the whole-domain budget with a warning instead of reaching mf6. See delta 2 |
| 15 | `aquifer_k_ms` / `porosity`, no bounds at all | Declared bounds `(1e-9, 1.0)` m/s and `(0.01, 0.7)`. Permissive by construction - wide enough never to bite a real pedotransfer value - but a non-numeric now refuses instead of reaching flopy |
| 16 | `compute_class: str = "standard"` | Param, door=CONSTANT |
| 17 | `RegionalWaterBudgetResult` (a `GraceModel` with `budget_layer` / `derived_params` / `summary`) | DELETED. The tool already serialized it with `.model_dump(mode="json")`; the dict is built directly and the wire shape is unchanged |
| 18 | `RegionalWaterBudgetScenarioError` / `RegionalWaterBudgetInputError` | DELETED (grep-to-zero). Input refusals keep `REGIONAL_WATER_BUDGET_INPUT_INVALID`; run failures now carry the archetype's own code. See delta 3 |
| 19 | `model_regional_water_budget_scenario` (the composer) | DELETED. Its phases are the shared step family |
| 20 | the 43-line hand-written LLM docstring | GENERATED by `render_docstring` from 7 Param descs + a 40-line `_DOC`; the routing front is budget-enforced at import |
| 21 | `TEMPLATE_CARD` / `AtomicToolMetadata` | kept verbatim |
| 22 | nothing persisted the run's own products | NEW: `chart_spec.json` + `metrics.json` under the run prefix |

## Inventory - SWMM `swmm_aquifer_baseflow_to_node`

| # | old behavior (`aquifer_baseflow.py` @ HEAD) | re-homing |
|---|---|---|
| 1 | `_geocode_site(location)` via `TOOL_REGISTRY` | `workflows/shared/site_resolve.geocode_point`, memoized; reached through `steps.site.resolve_site` |
| 2 | `lat`/`lon` override `location` | `steps.site.resolve_site`, unchanged; Params `lat` / `lon`, door=USER, optional, bounded |
| 3 | `site_latlon`, never surfaced at all | NEW Param, door=DERIVED, optional - the site is now a row on the form card |
| 4 | `derive_soil_column(lat, lon)` -> one 4-field column, or a refusal | FOUR Params (`porosity`, `wilting_point`, `field_capacity`, `conductivity_in_hr`), door=DERIVED, `steps.soil.*`, each `user_lever` + bounded, off ONE memoized texture read |
| 5 | "all four or none" - a partial explicit column was ignored and everything re-derived | Per-param doors: an explicitly supplied property wins at door 1 and the REST derive. See delta 4 |
| 6 | the three-branch `SyntheticInput` construction (`user` / `derived` / `default_demo`) | DELETED. The resolver builds all three cases from the declaration; the derived case carries its texture evidence via `Derived` |
| 7 | `gate_input_review(entries=[_col_entry])` | the plan's `FormGate`, which presents all 28 rows rather than one |
| 8 | `SWMM_PHYSICS_INPUT_REQUIRED` + `physics_refusal_reason` on an unresolved column | `steps.soil` refuses with the same code, naming the site AND the explicit-column way out |
| 9 | `default_two_storm_forcing(dt_min, sim_days)` - a baked demo hyetograph | `steps.two_storm_forcing`, shaped by FIVE declared Params (`storm_intensity_in_hr`, `storm_start_hr`, `storm_duration_hr`, `second_storm_day`, `sim_days`). The pattern is the QUESTION this template asks; the numbers are now labeled, bounded and editable |
| 10 | `rainfall_series_in_hr` coerced by a local `_coerce` returning `None` on garbage | Param, door=USER, optional, with `derived_when_absent` naming the declared storms; malformed input now refuses `SWMM_DECK_INVALID` instead of silently reverting to the demo storms. See delta 5 |
| 11 | `dt_min` / `area_ac` / `sim_days` clamped by `max(int(...), 1)` etc. | Params with declared bounds |
| 12 | `a1` / `b1` / `initial_water_table_ft` defaults on the signature | Params, door=SCENARIO, bounded, `a1` a `user_lever` |
| 13 | `surface_elev_ft=10.0`, a `build_aquifer_inp` kwarg no caller could reach | Param, door=CONSTANT |
| 14 | `[SUBCATCHMENTS] ... 5 ...` (imperviousness) baked in the f-string | Param `imperviousness_pct`, door=SCENARIO |
| 15 | `[INFILTRATION] S1 3.5 0.5 0.30` baked in the f-string | Params `soil_suction_in` / `infiltration_ksat_in_hr` / `initial_moisture_deficit`, door=CONSTANT. See the fork |
| 16 | `[AQUIFERS] ... Seep=0.002` baked in the f-string | Param `aquifer_seepage_in_hr`, door=SCENARIO |
| 17 | `[EVAPORATION] CONSTANT 0.02` baked in the f-string | Param `evaporation_in_day`, door=SCENARIO |
| 18 | `[SUBAREAS]`, `[JUNCTIONS]`, `[OUTFALLS]`, `[CONDUITS]`, `[XSECTIONS]`, `ROUTING_STEP`, subcatchment width/slope | KEPT literal in the deck writer, with the constraint stated: this is the schematic that lets a node hydrograph be read, not a property of the site. Documented decision, not an oversight |
| 19 | `build_aquifer_inp` | `steps.build_aquifer_inp`, deck text byte-identical at the declared defaults |
| 20 | `solve_aquifer_deck` - its own pyswmm loop, hard-wired to `J1` and `S1` | `steps/solve.solve_deck`, taking the sampled objects as declared arguments; reusable by the whole pyswmm family. The `S1` runoff series was collected and never read - DELETED |
| 21 | the two-variant orchestration (`a1` then `a1=0`) inside the tool body | TWO declared `Deck` + `Solve` step pairs. The control run is now a plan node, so the ledger can replay one solve while the chart re-executes |
| 22 | `_mean_between` / `_peak` / the tau fit / the bump window | `steps.baseflow_metrics`, moved verbatim |
| 23 | the hard-coded `6..11` dry window and `11.5..12 / 12..14` bump windows | `dry_window_start_day` / `dry_window_end_day` as CONSTANT Params; the +-0.5 d / 2 d bump spans stay in the metrics function as the DEFINITION of the statistic |
| 24 | `_node_chart_spec` + the inline `emit_chart` try/except | `.chart("node_hydrograph", builder=steps.build_baseflow_chart)` |
| 25 | `current_emitter()` + `hasattr(emitter, "emit_chart")` hand-wiring | DELETED (law 8) |
| 26 | `charts_emitted: int` | `chart_specs: list[str]`. It counted emissions it could not observe; the run reports what it BUILT. See delta 6 |
| 27 | `SWMM_AQUIFER_INVALID` on a bad numeric | the declared-bounds refusal; the catch-all is now `SWMM_AQUIFER_INTERNAL_ERROR` |
| 28 | `SWMM_AQUIFER_SOLVE_FAILED` | `SwmmSolveError` (`SWMM_SOLVE_FAILED`), carried through `StepFailedError` |
| 29 | `aquifer_provenance` prose assembled from the entry | `_column_provenance(ran)`, read off the run's own row |
| 30 | the 34-line hand-written docstring | GENERATED by `render_docstring` from 28 Param descs + a 40-line `_DOC` |
| 31 | the module docstring's citations | kept verbatim |

## Deliberate behavior deltas (six)

**1. MODFLOW: supplying BOTH `location` and `aoi_latlon` no longer refuses.** HEAD
demanded exactly one. The explicit point now wins (door 1) and the place name
names the run - which is what a caller who passes both plainly means, and it
matches how river_dye resolves the same collision.

**2. MODFLOW: an unknown `zone_partition` falls back instead of reaching mf6.**
HEAD passed any string through to the deck writer, which partitioned nothing. The
allow-list is `{upgradient_downgradient}` - the one scheme the writer implements -
and anything else logs and reports the whole-domain budget.

**3. MODFLOW: a run failure carries the ARCHETYPE's error code.** HEAD wrapped
everything as `REGIONAL_WATER_BUDGET_RUN_FAILED`. The step now re-raises the code
the archetype run itself reported, which is strictly more informative; the INPUT
refusal keeps its old code verbatim.

**4. SWMM: a PARTIAL explicit soil column is now honored.** HEAD required all four
properties or it derived all four, silently discarding e.g. a supplied porosity.
Each property is its own door now.

**5. SWMM: a malformed `rainfall_series_in_hr` REFUSES.** HEAD's `_coerce`
returned `None` on a bad pair and the run silently used the demo storms - the
swallow class ADR 0305 delta 5 removed from river_dye.

**6. SWMM: `charts_emitted` becomes `chart_specs`.** The old integer claimed
knowledge the tool did not have (`emit_chart_payloads` no-ops without an emitter
and reports nothing back). The list names the specs the run BUILT, which is the
product.

None of the six moves a number on the reference question; see the parity runs.

## THE FORK FOR NATE - the Green-Ampt trio (checkpoint answer #10)

Promoting `build_aquifer_inp`'s literals to declarations made law 9 refuse the
template in auto mode, because six of them are physics-shaped and had no real
source. They are now tagged per the repo's established reading of `consequence`
(`physics` = a SITE-SPECIFIC measured quantity a fetcher should have provided, as
with the carrier discharge and the aquifer K; `numerical`/`scenario` = a
documented or hypothetical value with a labeled default and declared bounds -
the treatment do_sag gives `k1_per_day`/`k2_per_day` and `channel_width_m`).
That restores HEAD's auto-mode behavior exactly.

The honest observation the checkpoint surfaced, which is a PHYSICS change and
therefore NATE's:

> The `[INFILTRATION]` Green-Ampt block writes suction 3.5 in, Ksat 0.5 in/hr
> and initial deficit 0.30 for the subcatchment SURFACE, while the
> `[AQUIFERS]` column beneath it is derived from SoilGrids at the same point
> (Ksat 0.1318 in/hr at Ames). One site, two conductivities, differing by 4x.
> The same texture fit already yields a Green-Ampt suction and a USDA texture
> class (`aquifer_resolve.usda_texture_class`, which the Landlab Green-Ampt
> chain uses for exactly this).

Options: (a) leave it, keeping the surface block a labeled literature default and
the caveat in the param descs (what landed); (b) derive the Green-Ampt trio from
the same texture fit, which makes the template internally consistent and moves
the answer - a physics change needing its own reference run; (c) derive it and
re-tag the trio `consequence="physics"`, which also makes the template refuse
where SoilGrids cannot serve. **Recommendation: (b)**, as the first job of the
SWMM engine-complete campaign rather than inside a checkpoint - it is exactly the
kind of purity work item 5 of the migration order exists for.

Queued alongside it: adopting the DERIVED door for river_dye's carrier discharge
(checkpoint answer #11), which closes ADR 0305's delta 1 with no library change.

## The shared families

### `workflows/modflow/steps/` (382 lines)

| module | lines | what it owns |
|---|---|---|
| `errors.py` | 48 | four typed failures, one of them sharing law 9's code |
| `aoi.py` | 57 | the AOI point + name derivations |
| `aquifer.py` | 85 | the memoized SoilGrids fit; K and porosity as `Derived` |
| `archetype.py` | 127 | `RunArchetype.<archetype>` - args, dispatch, typed-layer check, map load, run-prefix read |
| `products.py` | 32 | the budget chart builder |
| `__init__.py` | 33 | the public surface |

Ten MODFLOW templates call `_resolve_aoi_point`, `resolve_and_gate_aquifer` and
`_run_archetype` today - all three imported ACROSS templates from
`sustainable_yield`, which is why that file is 1,092 lines. This family is where
they belong.

### `workflows/swmm/steps/` (339 lines) + `aquifer_baseflow/steps.py` (371)

| module | lines | what it owns |
|---|---|---|
| `errors.py` | 40 | four typed failures |
| `site.py` | 54 | the site derivation (sheet-direct, so derivation order cannot matter) |
| `soil.py` | 119 | the memoized two-zone column; four `Derived` properties |
| `solve.py` | 95 | `Solve.pyswmm` - the shared headless run loop with declared sampling |
| `__init__.py` | 31 | the public surface |
| `aquifer_baseflow/steps.py` | 371 | this template's hook: the deck writer, the forcing, the metrics, the chart |

### `workflows/shared/`

- `site_resolve.py` (56, NEW) - the memoized geocode. It is engine-independent, so
  it is not in either steps family.
- `run_products.py` (61) - MOVED from `workflows/telemac/`. It was never
  TELEMAC-specific and MODFLOW needs it; both importers repointed, old path
  grepped to zero.

## R3 acceptance

Reference question, both engines, both runs: `Ames, Iowa` - deep agricultural
soil with unambiguous SoilGrids texture coverage, and the site the existing law-9
A/B proof already uses.

### (a) Inventory

The two tables above: 22 + 31 rows, six deliberate deltas, five symbols
documented-deleted (`model_regional_water_budget_scenario`,
`RegionalWaterBudgetResult`, `RegionalWaterBudgetScenarioError`,
`RegionalWaterBudgetInputError`, `solve_aquifer_deck`).

### (b) Old vs new - BIT-IDENTICAL, both engines

`modflow_regional_water_budget(location="Ames, Iowa",
zone_partition="upgradient_downgradient", compute_class="standard")`:

| | OLD (`b7e898f3`) | NEW (declarative) |
|---|---|---|
| `chd_in` | 9.887537099091208 m^3/day | **9.887537099091208** |
| `chd_out` | -9.88753734666957 m^3/day | **-9.88753734666957** |
| derived `aquifer_k_ms` | 9.298175630928423e-07 m/s | identical |
| derived `porosity` | 0.15691653512022236 | identical |
| layer bbox | `(-93.62921267786774, 42.01767770074262, -93.60465753416545, 42.03582715478343)` | identical |

`swmm_aquifer_baseflow_to_node(location="Ames, Iowa", sim_days=24, a1=0.002,
b1=1.0, area_ac=100, dt_min=15, initial_water_table_ft=4.0)`:

| | OLD (`b7e898f3`) | NEW (declarative) |
|---|---|---|
| soil column (por/wp/fc/K) | 0.4637 / 0.1963 / 0.3568 / 0.1318 | **identical** |
| peak node inflow, with GW | 2.30071 cfs @ 8.25 h | **2.30071 @ 8.25** |
| peak node inflow, no GW | 1.50835 cfs | **1.50835** |
| between-storms baseflow | 0.60124 vs 0.0 cfs | **0.60124 vs 0.0** |
| baseflow contribution | 0.60124 cfs | **0.60124** |
| recession tau | 703.15 h | **703.15** |
| storm-2 recharge bump | 1.49741 cfs | **1.49741** |
| flow-routing continuity | 0.0 % | **0.0** |

Byte-identity is the right expectation for both and was checked rather than
assumed: neither pipeline has an RNG, mf6 and SWMM 5 are deterministic for a
fixed deck, and the only fetched inputs (the geocode and the SoilGrids texture)
are static facts. One thing had to be fixed to GET it: the first attempt
memoized the pedotransfer call on coordinates rounded to five decimals, which
moved the sampled texture window by about a metre and shifted `chd_in` in the
fifth significant figure. The memo now keys on the exact resolved point.

### (c) Live `user_gated` drives through the harness

**MODFLOW - `scripts/drive_regional_water_budget_cards.py`, run
`01M0SQTZP1KVZ2GKGK1WM8XG86`, exit 0.**

- The FORM CARD fired with 7 rows, titled "Review the regional water-budget
  inputs". `aquifer_k_ms` arrived as `9.298175630928423e-07 m/s`, `door=derived`,
  `basis=derived`, badge `derived by aquifer_k_ms`, bounds `[1e-09, 1.0]`, note
  `DERIVED from SoilGrids texture at the AOI (sand=16.9%, clay=32.1%, 5-15cm) via
  the Saxton-Rawls (2006) pedotransfer function`. **That is the number ADR 0305's
  delta 1 said the card could not show.**
- **The edits reached the physics, provably, through two different surfaces.**
  The driver revised `porosity` 0.157 -> 0.25 and `aquifer_k_ms` to exactly twice
  the derived value. The run's own `metrics.json` records both revised values;
  and `chd_in` is **19.775073 m^3/day against the reference 9.887537 - a ratio of
  2.0000**, which is what doubling K does to a fixed-head gradient. Porosity is
  the control in the same experiment: a steady GWF flow budget is not a function
  of porosity, so the sheet is the only place it can show, and it shows there.
- The budget layer landed on the canvas as `Regional Water Budget (zonal
  partition)`, and the two SoilGrids inputs surfaced through the emit-on-fetch
  seam.
- The run's own products are in its prefix:
  `s3://trid3nt-runs/01M0SQTZP1KVZ2GKGK1WM8XG86/chart_spec.json` and
  `metrics.json`. One chart emission crossed the wire.

**SWMM - `scripts/drive_aquifer_baseflow_cards.py`, exit 0.**

- The FORM CARD fired with 28 rows, titled "Review the aquifer-baseflow
  scenario". All four two-zone column rows arrived `door=derived` with their real
  values (0.4637 / 0.1963 / 0.3568 / 0.1318), their bounds and the note naming
  the texture (`sand=16.9%, clay=32.1%`). Seven rows carried `advanced=True` for
  the fold: the three Green-Ampt parameters, `surface_elev_ft`, `dt_min` and the
  two measurement windows - which is exactly the set that should be behind a
  fold, and every one of them was an invisible literal at HEAD.
- **The edit reached the physics.** The driver revised `a1` 0.002 -> 0.004 and
  the chart the run itself emitted narrates
  `0.938 cfs baseflow between storms ... receding with tau ~374 h` against the
  reference run's `0.601 cfs` and `703 h`. Doubling the linear-reservoir
  discharge coefficient roughly halves the recession time constant and raises the
  early baseflow - the correct response, not a proportional one.
- No draw card, no plain warning: this template declares neither, exactly as
  its plan says.
- The assertion cites the run's OWN emitted chart payload
  (`require_chart(title_contains="node hydrograph")`), not a rebuild.

### (d) Net LOC - the raw truth

These are small templates. The point is SHAPE FIT, not a LOC win, and the
arithmetic says so.

| | before | after | delta |
|---|---|---|---|
| `regional_water_budget.py` | 360 | 304 | **-56** |
| `modflow/steps/` (new shared family) | 0 | 382 | +382 |
| **MODFLOW total** | **360** | **686** | **+326** |
| `aquifer_baseflow.py` | 560 | 405 | **-155** |
| `aquifer_baseflow/steps.py` (new) | 0 | 371 | +371 |
| `swmm/steps/` (new shared family) | 0 | 339 | +339 |
| **SWMM total** | **560** | **1,115** | **+555** |
| `declarative/` (4 seams) | 1,722 | 1,773 | +51 |
| `testing/live_run.py` | 365 | 389 | +24 |
| `shared/site_resolve.py` (new) | 0 | 56 | +56 |
| `shared/run_products.py` (moved) | 61 | 61 | 0 |
| `scripts/` (2 new drivers) | 0 | 200 | +200 |
| `scripts/` (2 proofs repointed) | 148 | 156 | +8 |
| `tests/` (new + repointed) | 1,165 | 1,395 | +230 |
| **everything touched** | **4,381** | **5,831** | **+1,450** |

Read it straight:

- **Both engines are net-POSITIVE, and that is the expected shape for a
  checkpoint.** The net-LOC law measures an engine FAMILY at completion; this
  wave migrates 1 of 15 SWMM templates and 1 of 15 MODFLOW templates while
  paying the whole one-time cost of both shared families up front. The same
  arithmetic held for TELEMAC: ADR 0303's wave was +1,613 and the family did not
  reach net-negative until ADR 0305.
- **Where the return lives, stated so it can be checked.** `modflow/steps/`
  extracts three functions that ten MODFLOW templates currently import ACROSS
  template boundaries from `sustainable_yield` - each carrying its own copy of
  the `try/except` mapping, the substep bracket and the double gate. `swmm/steps/`
  extracts a pyswmm run loop that six templates re-implement. Those 30 templates
  are the pool; this wave is what makes collapsing them a re-declaration.
- **The template files themselves both shrank** (-56 and -155) while gaining
  bounds, doors, editable rows and generated docstrings for values that were
  previously unreachable literals. That is the honest headline.

### Coded tools

No change. `modflow_regional_water_budget` and `swmm_aquifer_baseflow_to_node`
keep their single registry entries and their co-located `corpus.yaml` files
unchanged. No tool added, none removed.

### (e) TELEMAC parity re-runs

- **do_sag**, pinned at 2.0 m^3/s: DO minimum **8.5772 mg/L at 10631.7 m**,
  `violates=false`, 60 curve points - identical to ADR 0303's reference and ADR
  0305's re-run. The library changes moved nothing.
- **river_dye**: untouched. `tests/test_run_river_dye_scenario.py` green in the
  `[p-r]` slice.

## Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1639 passed, 5 skipped, **0 failed** |
| `tests/test_[f-o]*.py` | 6660 passed, 3 skipped, 1 xfailed, **6 failed** - the 4 baseline `test_fetch_resolution_gate` + the 2 `test_model_fire_spread_chain` failures ADR 0305 recorded at this same commit |
| `tests/test_[p-r]*.py` | 2122 passed, 2 skipped, **0 failed** |
| `tests/test_[s-z]*.py` | 1420 passed, 6 skipped, **0 failed** |
| `contracts/tests` | 729 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True` |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |

No `workers/` path changed: both engines here are host-exec (mf6 subprocess under
`TRID3NT_MODFLOW_LOCAL=1`; pyswmm in-process), so no image rebuild was needed and
none was done.

Additional proof runs, both green and both regenerating their committed
deliverables unchanged:

- `scripts/proof_swmm_aquifer_baseflow.py` - repointed onto the step family;
  reproduces 2.301 / 1.508 / 0.6012 / tau 703.1 / bump 1.497 exactly.
- `scripts/proof_law9_soil_column_ab.py` - repointed at the new refusal seam;
  A=PASS (column derived, solve completes), B=PASS (typed
  `SWMM_PHYSICS_INPUT_REQUIRED`, no solve on an invented column).

New offline coverage: `tests/test_declarative_generalization.py` (13 tests) -
plan validity for both templates, the structural law-9 check (no `physics` param
may rest on a labeled default), the generated routing views, and one test per
library seam added here.

## Consequences

- The checkpoint's verdict is that mass conversion may proceed. The library took
  four small additions and no redesign to fit two engines whose shapes differ
  from TELEMAC's in every way that was supposed to matter.
- The DERIVED door is now the answer to "how does a real-data value reach the
  form card". That is a doctrine change worth stating in the design doc, and it
  retires an open fork rather than adding one.
- Two shared step families exist for the two priority engines, each sized for the
  templates that come next.
- One physics fork is open and belongs to NATE (the Green-Ampt trio).

## CORRECTION (2026-08-24, post-review)

An adversarial review refuted this wave NARROWLY: the checkpoint verdict stands
(the library is not TELEMAC-shaped and mass conversion may proceed), two blocking
defects in what it landed did not, and several claims above were wrong or
imprecise. Corrections live here rather than in the text above - an ADR records
what was decided and what was found, so the original stays readable and this
section is what supersedes it. Every item below is landed and gated; nothing here
is a plan.

### B1 - the memo cached FAILURES (blocking, fixed)

`modflow/steps/aquifer._texture_fit` and `swmm/steps/soil._column` were
`lru_cache` wrappers over derivations that NEVER RAISE: `derive_soil_k` /
`derive_soil_column` return `(None, {"reason": ...})` when SoilGrids cannot
serve. So an upstream 503, timeout or rate-limit entered the cache as if it were
a fact about the site, and every later run at that point inherited the refusal
for the life of the daemon - an upstream provider error internalized as our own
verdict, which is precisely what the repo forbids.

Both now memoize ON SUCCESS ONLY, through one shared helper
(`workflows/shared/point_memo.memo_on_success`: bounded FIFO, keyed on the EXACT
point so the five-decimal-rounding fix recorded above still holds). Two tests pin
the retry: a flaky derivation refuses on attempt 1 and attempt 2 FETCHES AGAIN
and resolves, while a second param off the same resolved point still costs no
third fetch (`calls == 2` after all three reads). Reverting the helper to cache
every result turns both tests red, which is what makes them a pin.

For contrast, and because it is the reason the defect was easy to miss:
`shared/site_resolve._geocode` is also `lru_cache`d and was already correct - it
RAISES on failure, and `lru_cache` never caches an exception. The two soil
derivations differed only in returning their failure instead of raising it.

### B2 - the sheet-handback test could not fail (blocking, fixed)

`test_the_run_hands_back_the_sheet_it_actually_ran_on` ran in AUTO mode with no
revision, so `result.params` and the sheet passed in were the same values and
deleting `RunResult.params` would not have failed it - a test for the wave's
first library addition that could not observe it. It now drives a REVISING form
gate through the scripted card client, and asserts the three things that make the
seam real: the sheet the caller passed IN still reads the pre-review `1.0`, the
run hands back `7.5` with `basis=user`, and the step downstream of the gate ran on
`7.5`. Probe: removing `out.params` from `_reseat_after_gate` fails it with
`assert 1.0 == 7.5`.

The scripted client itself moved to `tests/card_client.py` (it was private to
`tests/test_declarative_cards.py`), so a second file driving a card reuses the
wire round trip rather than restating it.

### N1 - the derived badge now reads the EVIDENCE

Item 3 above says a `Derived`'s `real_source` reaches "the row the form card
renders", and the (c) drive recorded the badge as `derived by aquifer_k_ms`.
Both were true and neither was what the user needed: `form.source_badge` read
`real_source` only on `basis == "fetched"`, so a derived row's badge named the
function - which is the name of the row the user is already looking at.
A derived row with evidence now badges `derived from <real_source>`; without
evidence it still falls back to `derived by <fn>`. Live, at Ames:
`derived from fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)` on both MODFLOW
rows and `... (Saxton-Rawls 2006 two-zone column)` on all four SWMM rows.

### N2 - one rendering rule for the card and the provenance row

The provenance row rounded floats to six significant figures (item 4); the form
card did not round at all. Two surfaces described the same param and disagreed
about it. Both now render through `declarative.params.wire_value`, whose
docstring states the trade the rule makes: six significant figures keep
`9.298176e-07` honest and shorten a LARGE value (a latitude of `42.0176777`
renders `42.0177`, about 10 m). It is DISPLAY only - the run reads the sheet, and
`metrics.json` from the pinned parity re-run below still carries
`0.15691653512022236` in full.

### N3 - no fabricated run prefix

`testing/live_run._read_run_products` preferred `role="primary"` but FELL BACK to
the first raster, so a run whose only rasters are emit-on-fetch context layers
reported `run_id = "cache"` and then "the run's products are missing" - a
fabricated prefix dressed as a missing product. The fallback is gone: no primary
raster is now the same honest sentence as no raster at all, plus the count of
context rasters that were there. Both shapes are pinned offline
(`tests/test_live_run_harness.py`), and both appear in the re-run evidence: the
SWMM run reports `no published PRIMARY raster to locate the run prefix (2 context
raster(s) on the canvas)`, while the MODFLOW run locates
`01M0SXN4J5B285T5DQFVH7SYE0` from its primary layer with the same two SoilGrids
context rasters ahead of it.

### N4 / N11 - the evidence is persisted, and regenerates

Both card drives now write their evidence into `docs/proof/` by default instead
of `/tmp`, and both were re-run at this correction against a cold daemon:
`docs/proof/swmm_aquifer_baseflow_cards_evidence.json` (exit 0; 28 form rows; the
run's own chart payload, which for this chart-first template IS the product) and
`docs/proof/modflow_regional_water_budget_cards_evidence.json` (exit 0; ratio
2.0000). The SWMM half is no longer a report of a run that left nothing behind.

`docs/proof/*` is gitignored (only `docs/proof/templates/` is committed), so the
files themselves are local artifacts like every other proof JSON there. What is
COMMITTED is the drive that writes them, with that path as its default - which is
what makes the record reproducible rather than a paste in a report.

### N5 - `modflow/steps/aquifer.py`'s module docstring

It still described the pre-fix behaviour ("memoized on the rounded point") and
over-claimed the badge. Corrected to what the code does.

### N6 - the design doc rule, restated, with the gap it leaves

`docs/design/declarative-workflows.md` said "a point SAMPLE is a derived Param".
The rule is about what the PLAN CONSUMES: a scalar that fits a form cell - a
point sample, a basin mean, a class fraction, a station statistic. Restated
there, with a recorded GAP that item 11 above did not mention: what a
world-reading derivation FETCHES on its way to that scalar sits outside the
`Data` machinery - not ledgered (a resume re-fetches), not an artifact the
interpreter can evict on a form revision (the derivation re-runs instead, and its
memo decides whether the fetch repeats), not walked by the terminal leaked-ref
scan. Small today; a decision the engine campaigns own, not silently blessed
doctrine.

### N7 - what the DERIVED door costs, stated plainly

Item 11 sells the door without its price. Derivations run inside
`resolve_params`, which the tool body calls BEFORE `interpret` - therefore before
`validate_plan` and before the law-9 floor. A run that is about to refuse for
invented physics, or whose plan is invalid, has already paid for every derivation
fetch. That is the last-honest-moment trade: the card can only show a real number
if the number was fetched before the card. Reordering (validate the plan, then
derive) is QUEUED as a library follow-up and deliberately NOT implemented here -
it changes when every declared workflow fetches, which is not a correction.

### N9 - the gate table above is wrong in two places

Re-observed at this commit, not re-reported:

- `[f-o]`: **4 failed / 6662 passed** at HEAD - the 4 baseline
  `test_fetch_resolution_gate` failures ONLY. The 2 `test_model_fire_spread_chain`
  failures the table names were GREEN at this commit; the ADR carried them
  forward from ADR 0305 without re-observing them.
- `[a-e]`: **1652 passed**, not 1639 - the count predates the wave's own 13 new
  tests.

### N12 / N13 - narrative and process

- The emit-on-fetch SoilGrids layers were re-checked live. BOTH runs surface
  them: the MODFLOW bullet in (c) is correct as written, and the SWMM run's two
  context rasters are the same seam (they are what N3 is about). One caveat worth
  recording, since it made the first re-run look otherwise: the derivation memo
  is process-wide, so a SECOND run at the same point inside one daemon does not
  re-fetch and therefore does not re-surface the context layers. The committed
  evidence is from a cold daemon.
- Process footnote: `tests/test_declarative_generalization.py` landed in the DOCS
  commit (`5957eaba`), not with the code it tests. The tests for a wave belong in
  the wave's code commits; a docs commit that carries tests is how an insensitive
  test (B2) reaches HEAD without a reviewer seeing it beside its seam.

### Re-run gates (the correction's own)

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1654 passed, 5 skipped, **0 failed** |
| `tests/test_[f-o]*.py` | 6664 passed, 3 skipped, 1 xfailed, **4 failed** - the baseline `test_fetch_resolution_gate` four, nothing else |
| `tests/test_[p-r]*.py` | 2122 passed, 2 skipped, **0 failed** |
| `tests/test_[s-z]*.py` | 1420 passed, 6 skipped, **0 failed** |
| `contracts/tests` | 729 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True` |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |
| `scripts/drive_aquifer_baseflow_cards.py` | exit 0; 0.938 cfs baseflow, tau ~374 h at the revised `a1` |
| `scripts/drive_regional_water_budget_cards.py` | exit 0; `chd_in` 19.775073 = 2.0000x the reference at the revised K |

The counts are +2 in `[a-e]` (the two retry tests) and +2 in `[f-o]` (the two
run-prefix tests) against N9's corrected baselines.

**MODFLOW pinned parity, un-edited at the DERIVED K** (run
`01M0SXCN3JYK98ZHA95NEV5AK7`, read off its own `metrics.json`):

| | ADR reference | this correction |
|---|---|---|
| `chd_in` | 9.887537099091208 | **9.887537099091208** |
| `chd_out` | -9.88753734666957 | **-9.88753734666957** |
| `aquifer_k_ms` | 9.298175630928423e-07 | identical |
| `porosity` | 0.15691653512022236 | identical |

Nothing in this correction moved a number on either engine's reference question.
