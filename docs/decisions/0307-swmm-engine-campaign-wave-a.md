# ADR 0307 - SWMM engine campaign, wave A: the standalone solve templates

Status: LANDED (wave 5 of the declarative campaign; wave A of the SWMM
engine-complete campaign)
Design: `docs/design/declarative-workflows.md` (migration-order item 5)
Builds on: ADR 0303 (library v1 + do_sag), ADR 0304 (form + draw cards),
ADR 0305 (river_dye + the live-run harness), ADR 0306 (the generalization
checkpoint + its correction - the seam rules this wave declares against:
memo-on-success, the DERIVED-door rule, `wire_value`, `RunResult.params`)

## Context

The checkpoint passed and cleared mass conversion. SWMM is NATE's top-priority
engine, so it goes first. This wave migrates the tranche the kickoff scoped: the
templates that make a REAL solve, are not giants, and are not entangled in a
shared composer that would have to be migrated with them.

That tranche turns out to be exactly TWO templates, and the reasoning for that
number is the first thing this ADR owes.

## THE TRANCHE, AND WHY IT IS TWO RATHER THAN THREE OR FOUR

Fifteen SWMM templates are registered (one `corpus.yaml` each). One is migrated
(`swmm_aquifer_baseflow_to_node`, ADR 0306). The remaining fourteen fall into
exactly four groups, and the group boundaries - not template size - are what set
the wave boundaries:

| group | templates | shared machinery underneath | lines |
|---|---|---|---|
| **A. standalone solve** | `swmm_rdii_rtk_unit_hydrograph`, `swmm_snowmelt_degree_day` | none - each owns its deck writer and its solve | 461 + 512 |
| **B. published deck** | `swmm_lid_raingarden_wq`, `swmm_wwtp_detention_ponds`, `swmm_pump_pid_rtc` | `workflows/swmm/deck_runner` (393) + `mesh/swmm_deck_runner` (625) | 117 + 110 + 110 |
| **C. mechanism comparison** | `swmm_subcatchment_runoff_comparison`, `swmm_node_hydraulics_comparison`, `swmm_wetwell_pump_control_comparison`, `swmm_lid_performance_comparison`, `swmm_wq_buildup_washoff_comparison` | `workflows/swmm/mechanism_compare` (163) + `mesh/swmm_mechanism_compare` (981) | 106 + 104 + 101 + 121 + 106 |
| **D. the AOI giants** | `swmm_urban_flood`, `swmm_network_import`, `swmm_dual_drainage` | each other (dual_drainage is built ON the other two) | 1,610 + 658 + 457 |

Groups B and C are each a family of thin composers over ONE runner. Migrating one
member means either dragging the whole runner in for a single caller, or keeping
the old runner alive beside the new declared steps until the last sibling lands -
two machineries doing one job, which is the state the campaign exists to end.
Group D is explicitly out of scope per the kickoff, and `dual_drainage` goes with
it because it is built on the other two giants' machinery.

That leaves group A: the two templates with a real solve, a real question, no
shared runner behind them, and no giant in front of them. **Both are migrated
here.** Taking a third would have meant half-migrating a family; the kickoff's
"3-4" is a target for the size of a wave, and this engine's remaining set does
not divide that way.

Neither template touches the open Green-Ampt fork (ADR 0306 answer #10, NATE's).
That fork is about an INTERNAL INCONSISTENCY: `aquifer_baseflow` derives a soil
column from SoilGrids at a real site and writes a different, literature-default
conductivity into the surface `[INFILTRATION]` block at the same point. Neither
template here derives anything from real soil - both are schematic decks with
declared forcing - so there is no second conductivity to contradict. Their
infiltration blocks are labeled literature defaults with declared bounds, tagged
`numerical`, which is the treatment the repo already ruled for that class.

### Projected remaining waves

| wave | scope | what it is FOR, beyond the migration |
|---|---|---|
| **A (this)** | `rdii_rtk`, `snowmelt_degree_day` | the shared family grows the pieces the giants will reuse: multi-attribute sampling on one solve, the one line-chart spec builder, the time-series/clock helpers |
| **B** | the published-deck trio + `deck_runner` + the `mesh/swmm_deck_runner` seam | the SWMM family's FIRST `Data` declaration. A published deck fetched at runtime IS an artifact, and an AUTHORED one, so it is `.byo()`-able - which is the whole point of the Data/BYO axis and SWMM has not exercised it once |
| **C** | the mechanism-comparison five + `mechanism_compare` + `mesh/swmm_mechanism_compare` | the PURITY FORK gets its per-engine answer. All five at once, because the ruled demo-script pattern (deck/params as INPUT, canonical values as a banner-labeled saved invocation) applies to the SHARED deck-authoring core, not to any one caller |
| **D** | `swmm_urban_flood`, `swmm_network_import`, `swmm_dual_drainage` | the AOI half: `Data` producers for 3DEP/OSM/Atlas-14, byo-mesh adoption, declared render steps on a real raster product. Where the family's net-LOC return is actually realised |

## Inventory - `swmm_rdii_rtk_unit_hydrograph`

Every variable and behavior at HEAD (`c884ab73`), and where it went.

| # | old behavior (`rdii_rtk.py` @ HEAD) | re-homing |
|---|---|---|
| 1 | `R1..K3` as nine signature defaults, no bounds | nine Params, door=SCENARIO, bounded, the three R's `user_lever`. Their `consequence` is `scenario`, NOT `physics`: R/T/K are CALIBRATION parameters fitted to flow monitoring and no fetcher serves them, which is the do_sag `k1_per_day` reading of law 9 |
| 2 | `uhs = [...] if R > 0 and T > 0 and K > 0`, then `return {"status":"error", ...}` on an empty list | `steps.closed_form_rdii`, raising `SwmmDeckError(error_code="SWMM_RDII_RTK_INVALID")`. Same code, now a typed step failure the interpreter carries |
| 3 | `area = max(float(sewershed_area_ac), 0.01)` | Param, bounds `(0.01, 1e6)` - the clamp IS the declaration |
| 4 | `depth = max(float(rainfall_depth_in), 0.0)` | Param, bounds `(0.0, 100.0)` |
| 5 | `dur = max(float(storm_duration_hr), dt_min/60)` | Param with bounds `(0.0, 240.0)`; the one-timestep floor stays in the step, where it is a property of the discretisation rather than of the storm |
| 6 | `c = min(max(float(direct_runoff_coeff), 0), 1)` | Param, bounds `(0.0, 1.0)` |
| 7 | `dt_min_i = max(int(dt_min), 1)` | Param, door=CONSTANT, bounds `(1, 60)` - the "advanced" fold |
| 8 | the `try/except (TypeError, ValueError)` wrapping the whole coercion block | DELETED. `resolver._finish` refuses a non-numeric bounded param by name, with its bounds and units |
| 9 | `rainfall_series_in_per_hr` expanded by a bare `float(hourly_in)` inside a loop, outside any `try` | Param, door=USER, optional, `derived_when_absent` naming the uniform design storm; `steps._hourly_series` refuses `SWMM_DECK_INVALID`. HEAD raised an untyped `ValueError` out of the tool |
| 10 | `n_storm` and the explicit branch's `intensity` - assigned, never read | DELETED (both branches) |
| 11 | `rtk_unit_hydrograph`, `rdii_hydrograph`, `_rdii_volume_cf`, `_rtk_expected_volume_cf` | `steps.*`, moved verbatim; the two private ones lose their underscore because a step module's surface is what the plan and the tests read |
| 12 | `_ACRE_IN_PER_HR_TO_CFS = 1.008389` | KEPT, with the constraint stated: it is a UNIT CONVERSION (SWMM's own), not a scenario value |
| 13 | `build_rtk_rdii_inp` | `steps.build_rtk_rdii_inp`, deck text BYTE-IDENTICAL (checked, not assumed - see acceptance (b)) |
| 14 | the deck's `[JUNCTIONS]`/`[OUTFALLS]`/`[CONDUITS]`/`[XSECTIONS]`, `INFILTRATION HORTON`, `FLOW_ROUTING KINWAVE`, the `N2` 400 ft pipes | KEPT literal in the deck writer with the constraint stated: this is the schematic that lets a node inflow be READ, not a property of any sewershed. Same decision as `aquifer_baseflow` row 18 |
| 15 | the `TS_RAIN` clock/value f-string | `steps.timeseries_block` + `steps.clock` (shared); byte-identical at precision 5 |
| 16 | `_solve_swmm_node_rdii` - its own pyswmm loop hard-wired to `N1` | DELETED. `steps/solve.Solve.pyswmm` with `nodes=("N1",)` declared; the node name is a module constant the plan reads |
| 17 | `cross_check_swmm: bool = True` + the `if cross_check_swmm:` block + `except Exception: logger.warning(...)` | DELETED. The cross-check is now a DECLARED `Deck` + `Solve` pair. See delta 1 and delta 2 |
| 18 | the direct-runoff rational-method list comprehension, inline in the tool body | `steps.rdii_metrics` |
| 19 | `max(...)` for the three peaks | `steps.peak` (shared), which also carries the INDEX |
| 20 | `build_rdii_chart_spec` + the inline `emit_chart` try/except + `build_chart_payload` | `.chart("rdii_vs_runoff", builder=steps.build_rdii_chart)`; the vega-lite shape comes from the shared `steps.line_chart_spec` |
| 21 | `current_emitter()` + `hasattr(emitter, "emit_chart")` hand-wiring | DELETED (law 8 - the interpreter owns emission) |
| 22 | `chart_emitted: bool` | `chart_specs: list[str]`. Same correction as ADR 0306 delta 6: it reported a claim about a card it could not observe; the list names what the run BUILT |
| 23 | the solved deck's continuity error, computed by the engine and discarded | NEW: `flow_routing_error_pct` on the result. The cross-check deck earned a mass balance and HEAD threw it away |
| 24 | `EPA_TABLE_7_1_AREA_AC`, `EPA_TABLE_7_1_SUM_R`, `EPA_TABLE_7_1_RAINFALL_IN_PER_HR`, `EPA_TABLE_7_1_PUBLISHED_RDII_CFS` - four module-level demo constants in workflow code | EXILED to `scripts/demo_swmm_rdii_epa_table_7_1.py`, a banner-labeled saved path-A invocation (the purity rule). The test and the proof renderer both import them from there, so the published numbers have ONE home and it is a demo |
| 25 | the 29-line hand-written LLM docstring | GENERATED by `render_docstring` from 15 Param descs + a 30-line `_DOC`; the routing front is budget-enforced at import |
| 26 | `TEMPLATE_CARD` / `AtomicToolMetadata` | kept (the card's `knobs` line updated for the dropped `cross_check_swmm`) |
| 27 | no gate at all - the tool had no input-review surface | the plan's `FormGate`, 15 rows |
| 28 | no run levers | `input_mode` + `restart_clean`, documented under `controls` |

## Inventory - `swmm_snowmelt_degree_day`

| # | old behavior (`snowmelt_degree_day.py` @ HEAD) | re-homing |
|---|---|---|
| 1 | `default_rain_on_snow_forcing(dt_min)` - a baked 5-day Buffalo demo series: the 20 F cold spell, the 45 F warm level, the 48-60 h ramp, the 12-36 h snowfall at 0.05 in/hr, the 60-72 h rain burst at 0.15 in/hr, the 120-hour window | DELETED as a function. TWELVE declared Params (`sim_days`, `cold_temp_f`, `warm_temp_f`, `warmup_start_hr`, `warmup_end_hr`, `snowfall_start_hr`, `snowfall_end_hr`, `snowfall_intensity_in_hr`, `rain_start_hr`, `rain_end_hr`, `rain_intensity_in_hr`, plus `dt_min`) drive `steps.rain_on_snow_forcing`. The PATTERN is the question this template asks; the numbers are now labeled, bounded and editable. Same treatment as `aquifer_baseflow` row 9 |
| 2 | the forcing built inside the tool, then passed to three deck calls | ONE declared `Forcing` step the three `Deck` steps `Ref`. "Three variants on the SAME forcing" is now a property of the plan value rather than a claim in the docstring |
| 3 | `_coerce(series)` returning `None` on garbage, so a malformed series silently reverted to the demo forcing | `steps.coerce_series` (shared), refusing `SWMM_DECK_INVALID`. The swallow class ADR 0305 delta 5 and ADR 0306 delta 5 removed elsewhere. See delta 9 |
| 4 | `temp = temp or dtemp` / `rain = rain or drain` - each series independently falls back | PRESERVED, in the forcing step: one series may be real observations while the other stays declared, which is exactly what the live proof does |
| 5 | `dt_min_i = max(int(dt_min), 1)`, `area = max(float(area_ac), 0.01)` | Params with declared bounds |
| 6 | the `try/except (TypeError, ValueError)` around the numeric coercion | DELETED - `resolver._finish` refuses by name |
| 7 | `cmin=0.001`, `cmax=0.01` as `build_snowmelt_inp` kwarg defaults AND signature defaults | Params, door=SCENARIO, bounded, both `user_lever` |
| 8 | `base_temp_f`, `dividing_temp_f`, `percent_impervious`, `plow_threshold_in`, `plow_fraction` | Params, door=SCENARIO, bounded |
| 9 | `plow_out_fraction=1.0`, a `build_snowmelt_inp` kwarg no caller could reach | Param, door=CONSTANT |
| 10 | `[SNOWPACKS]` surface fields baked in the f-string: FWF `0.10`, SD0 `0`, FW0 `0`, SD100 `2.0` | Params `free_water_fraction` / `initial_snow_depth_in` / `initial_free_water_in` / `depth_at_full_cover_in`. `initial_snow_depth_in` is SCENARIO (an antecedent state a caller would set); the other three are CONSTANT |
| 11 | `[TEMPERATURE] SNOWMELT {divide} 0.5 0.6 500 43.0 0` - five physics-shaped numbers inside the f-string | Params `ati_weight`, `negative_melt_ratio`, `site_elevation_ft`, `site_latitude_deg`, `longitude_correction_min`. The latitude drives the seasonal melt-coefficient ramp and is tagged `consequence="aoi"`; the elevation and latitude defaults are labeled as the Buffalo snowbelt the declared forcing describes |
| 12 | `[EVAPORATION] CONSTANT 0.0` baked in the f-string | Param `evaporation_in_day`, door=SCENARIO. Zero IS the winter case, and saying so is different from hiding it |
| 13 | `[INFILTRATION] S1 3.0 0.5 4 7 0` (Horton) baked in the f-string | Params `horton_max_rate_in_hr` / `horton_min_rate_in_hr` / `horton_decay_per_hr` / `horton_dry_time_days`, door=CONSTANT, tagged `numerical` with descs naming them literature defaults NOT fitted to a site. NOT the Green-Ampt fork: nothing here derives a soil column, so there is no second conductivity to contradict |
| 14 | `[SUBCATCHMENTS] ... 500 0.5 0`, `[SUBAREAS] S1 0.01 0.10 0.05 0.05 25 OUTLET`, the single `OUT` outfall, `FLOW_ROUTING KINWAVE` | KEPT literal in the deck writer, with the constraint stated: the schematic that lets a runoff hydrograph be read. Same decision as `aquifer_baseflow` row 18 |
| 15 | `build_snowmelt_inp` | `steps.build_snowmelt_inp`, deck text BYTE-IDENTICAL in both the plain and the REMOVAL variant (checked - see acceptance (b)) |
| 16 | the two `TSER_T` / `TSER_R` f-strings | `steps.timeseries_block` (shared) at precisions 3 and 4; byte-identical |
| 17 | `solve_snowmelt_deck` - its own pyswmm loop returning a 5-tuple, hard-wired to `S1` and to three attributes | DELETED. `steps/solve.Solve.pyswmm` with `subcatchments=("S1",)` and `subcatchment_attrs=("snow_depth","runoff","rainfall")` declared. Extending the SHARED loop to multiple attributes per object is this wave's main gift to the family |
| 18 | `sim.runoff_error` as the only continuity reported | the shared solve reports BOTH `runoff_error_pct` and `flow_routing_error_pct`; this template's answer keeps naming the runoff one (`continuity_error_pct`), which is the half a subcatchment question is judged on |
| 19 | `dividing_temp_f=-99.0` written inline at the rain-only call site | `steps.RAIN_ONLY_DIVIDING_TEMP_F`, with its docstring stating it is a DEFINITION of the control, not a value anyone would set |
| 20 | the three-variant orchestration inside the tool body, with `snow_removal` gating the third | THREE declared `Deck` + `Solve` pairs. The plow variant is no longer optional - see delta 7 |
| 21 | `_total_melt_in`, `_peak` | `steps.peak` (shared); the melt sum moved into `steps.snowmelt_metrics` as the DEFINITION of that statistic |
| 22 | the cold-period artifact computation, inline in the tool body | `steps.snowmelt_metrics`, reading the forcing step's own temperature series |
| 23 | `_swe_chart_spec` + `_runoff_chart_spec` + the two-iteration `emit_chart` loop | `.chart("swe_series", ...)` + `.chart("runoff_snowmelt_vs_rain_only", ...)`, both over the shared `steps.line_chart_spec` |
| 24 | `current_emitter()` + `hasattr` hand-wiring | DELETED (law 8) |
| 25 | `charts_emitted: int` | `chart_specs: list[str]` (ADR 0306 delta 6) |
| 26 | `removal_peak_swe_in` / `removal_runoff_peak_cfs` as `None` when the variant was skipped | always present. See delta 7 |
| 27 | the 42-line hand-written docstring | GENERATED by `render_docstring` from 32 Param descs + a 33-line `_DOC` |
| 28 | the module docstring's citations | kept verbatim, with the demo-forcing sentence corrected to describe declared params |
| 29 | no gate at all | the plan's `FormGate`, 37 rows, 12 of them behind the "advanced" fold |
| 30 | no run levers | `input_mode` + `restart_clean` |

## Deliberate behavior deltas (nine)

**1. RDII: the native-SWMM cross-check is no longer optional.** HEAD had
`cross_check_swmm: bool = True`. The plan declares the cross-check deck and solve
unconditionally.

The reason is structural, and it is worth stating because the same shape recurs:
a `When`-guarded step is only visible INSIDE its branch (the validator's Ref
scoping), so an answer step that must `Ref` the guarded solve cannot be written
outside it. The alternatives were to hide the branch inside a composite - which
is what the library exists to undo - or to declare it. It is also the honest
call on the merits: the module docstring calls the cross-check one of the
template's TWO acceptance checks, and a run that reported the closed form without
the engine it is validated against would be reporting an unvalidated number as a
validated one. The fast path that `cross_check_swmm=False` served is now the
closed-form STEP, which the offline tests call directly.

**2. RDII: a cross-check solver failure REFUSES.** HEAD wrapped the whole
cross-check in `except Exception: logger.warning(...)` and returned
`status="ok"` with `swmm_rdii_peak_cfs: null`. The solve is a declared
consequential step now, so its typed `SWMM_SOLVE_FAILED` reaches the caller.

**3. RDII: a malformed `rainfall_series_in_per_hr` refuses typed.** HEAD's
`float(hourly_in)` sat outside any `try`, so bad input escaped the tool as a raw
`ValueError`.

**4. RDII: `flow_routing_error_pct` is reported.** The cross-check deck always
earned a mass balance; HEAD discarded it.

**5. Both: `chart_emitted` / `charts_emitted` become `chart_specs`.** ADR 0306
delta 6, applied.

**6. Both: declared bounds now clamp, and a non-numeric refuses.** Neither
template had a bound on anything. The bounds are permissive by construction -
wide enough never to bite a real value - but `R1="lots"` no longer reaches numpy.

**7. Snowmelt: the plow-removal variant always runs.** HEAD had
`snow_removal: bool = True` gating the third solve, so
`removal_peak_swe_in` / `removal_runoff_peak_cfs` were `None` when it was off.
Same structural reason as delta 1 - the metrics step must `Ref` the plowed solve -
and the same merits: the plow comparison is named in the template's own question,
the deck is five days of hourly steps on one subcatchment, and the DEFAULT path
is unchanged. Only a caller who explicitly passed `snow_removal=False` pays for
the third solve, and gets a number instead of a `null`.

**8. Snowmelt: the physical run and the rain-only control differ in exactly one
declared argument.** They always did, but it was two adjacent calls in a
function; now it is two plan nodes whose kwargs differ in one place, which is
what makes the control auditable rather than asserted.

**9. Snowmelt: a malformed forcing series refuses.** HEAD's `_coerce` returned
`None` and the run silently used the baked demo forcing - the swallow class.

None of the nine moves a number on either reference question; see the parity runs.

## Purity: what is left inline

Grep-verified across both migrated templates: **zero demo constants remain in
workflow code.**

- `rdii_rtk.py` and `snowmelt_degree_day.py` carry no module-level numeric
  constants at all beyond `_SAMPLED` (three attribute NAMES).
- `rdii_rtk/steps.py` carries `_ACRE_IN_PER_HR_TO_CFS` (a unit conversion) and
  `NODE = "N1"` (a deck object name).
- `snowmelt_degree_day/steps.py` carries `SUBCATCHMENT = "S1"` (a deck object
  name) and `RAIN_ONLY_DIVIDING_TEMP_F` (the DEFINITION of the control variant).
- The EPA Table 7-1 numbers live in `scripts/demo_swmm_rdii_epa_table_7_1.py`,
  which is a banner-labeled saved invocation; the test and the proof renderer
  both import from it, so there is one declaration rather than three copies.
- What stays literal in each deck writer is the drainage/subcatchment
  SCAFFOLDING, named as such in the writer's docstring: the junctions, the
  outfall, the pipes, the subarea roughness, the subcatchment width and slope.
  That is the `aquifer_baseflow` row-18 decision applied consistently, not an
  oversight.

The structural law-9 guard from ADR 0306 is extended to the migrated set: each
template's test file asserts that NO param tagged `consequence="physics"` rests
on a SCENARIO or CONSTANT door. Both pass with an empty offender list, because
neither template claims a site measurement it does not have.

## The shared family, after this wave

`workflows/swmm/steps/` grew the three pieces the giants will reuse.

| module | before | after | what changed |
|---|---|---|---|
| `solve.py` | 95 | 110 | MULTI-ATTRIBUTE sampling (`node_attrs` / `subcatchment_attrs`), so a snowpack question reading `snow_depth` + `runoff` + `rainfall` costs ONE solve; BOTH continuity errors always reported, because a template asks about one or the other and computing the pair is free |
| `series.py` | 0 | 64 | `clock`, `coerce_series`, `timeseries_block`, `peak` - three spellings of the same `H:MM` formatting, three coercions of the same argument and three copies of argmax, collapsed |
| `charts.py` | 0 | 73 | `line_chart_spec` - the ONE multi-series line spec the family draws, with the honesty floor (fewer than two points is no chart) in one place |
| `__init__.py` | 31 | 35 | the public surface |
| `errors.py`, `site.py`, `soil.py` | 213 | 213 | untouched |

`aquifer_baseflow/steps.py` was repointed onto all three and SHRANK by 18 lines
while staying bit-identical (acceptance (e)). That is the first evidence that the
family pays back rather than just accumulating.

Five call sites for `line_chart_spec` exist today (two in snowmelt, one each in
rdii, aquifer_baseflow); the two remaining chart builders in the family
(`deck_runner._line_chart`, `mechanism_compare._overlay_chart`) are waves B and C.

## R3 acceptance

### (a) Inventory

The two tables above: 28 + 30 rows, nine deliberate deltas, eight symbols
documented-deleted (`_solve_swmm_node_rdii`, `build_rdii_chart_spec`,
`EPA_TABLE_7_1_*` x4 as workflow constants, `default_rain_on_snow_forcing`,
`solve_snowmelt_deck`, `_swe_chart_spec`, `_runoff_chart_spec`, `_total_melt_in`,
`_peak` x2, `_coerce`).

### (b) Old vs new - BIT-IDENTICAL, both templates

Both are deterministic by construction (no RNG, SWMM 5 is deterministic for a
fixed deck, and neither template fetches anything), so byte identity is the right
expectation - and it was CHECKED rather than assumed, at three levels: the deck
TEXT, the result scalars, and the full curve arrays.

**Deck text.** Both deck writers reproduce HEAD's output byte-for-byte at the
declared defaults, diffed against the HEAD module loaded side by side:

| deck | byte-identical |
|---|---|
| `build_rtk_rdii_inp` (3 UHs, 4 rain steps, dt=15, 100 ac) | **yes** |
| `build_snowmelt_inp` (`removal=False`) | **yes** |
| `build_snowmelt_inp` (`removal=True`) | **yes** |
| `rain_on_snow_forcing` vs `default_rain_on_snow_forcing(60)` | **yes**, both series |

Getting there needed three formatting decisions to be deliberate rather than
incidental: `free_water_fraction` renders `:.2f` so `0.10` stays `0.10`, the
latitude and the two Horton rates render bare so `43.0` / `3.0` / `0.5` keep their
trailing zero, and the elevation, the ATI weight and the two integer-valued Horton
constants render `:g` so `500.0` stays `500`. A deck that changed its own
numeric formatting would not have changed its answer, but it would have made the
identity claim unverifiable, and the point of the claim is that it is checkable.

**`swmm_rdii_rtk_unit_hydrograph()` at the declared defaults:**

| | OLD (`c884ab73`) | NEW (declarative) |
|---|---|---|
| `sum_R` | 0.19 | **0.19** |
| `rdii_peak_cfs` | 3.2951 | **3.2951** |
| `rdii_volume_cf` | 68973.8 | **68973.8** |
| `rtk_volume_identity_ratio` | 1.00006 | **1.00006** |
| `swmm_rdii_peak_cfs` | 3.2804 | **3.2804** |
| `swmm_vs_closed_form_peak_ratio` | 0.9955 | **0.9955** |
| `direct_runoff_peak_cfs` | 30.0 | **30.0** |
| `rdii_fraction_of_total` | 0.099 | **0.099** |
| `curves` (3 arrays x 244 points) | - | **identical, element for element** |

**The EPA Table 7-1 invocation** (10 ac, the published hourly rainfall, the
representative R/T/K split - now driven from the demo script):

| | OLD | NEW |
|---|---|---|
| `sum_R` | 0.36 | **0.36** |
| `rainfall_depth_in` | 2.0500000000000003 | **2.0500000000000003** |
| `rdii_peak_cfs` | 1.0599 | **1.0599** |
| `rdii_volume_cf` | 26790.9 | **26790.9** |
| `rtk_volume_identity_ratio` | 1.00006 | **1.00006** |
| `swmm_rdii_peak_cfs` | 1.0534 | **1.0534** |
| `swmm_vs_closed_form_peak_ratio` | 0.9938 | **0.9938** |
| `rdii_fraction_of_total` | 0.3063 | **0.3063** |
| `curves` (3 arrays x 156 points) | - | **identical** |

**`swmm_snowmelt_degree_day()` at the declared defaults:**

| | OLD (`c884ab73`) | NEW (declarative) |
|---|---|---|
| `peak_swe_in` | 1.2 | **1.2** |
| `total_melt_in` | 1.2 | **1.2** |
| `final_swe_in` | 0.0 | **0.0** |
| `snowmelt_runoff_peak_cfs` @ hr | 7.329 @ 71.0 | **7.329 @ 71.0** |
| `rain_only_runoff_peak_cfs` @ hr | 6.0502 @ 71.0 | **6.0502 @ 71.0** |
| `rain_on_snow_peak_amplification` | 1.2114 | **1.2114** |
| `cold_period_runoff_fraction_rain_only` | 0.3894 | **0.3894** |
| `removal_peak_swe_in` | 0.502 | **0.502** |
| `removal_runoff_peak_cfs` | 6.1781 | **6.1781** |
| `continuity_error_pct` | 0.0 | **0.0** |
| `curves` (5 arrays x 167 points) | - | **identical** |

A second snowmelt invocation (`area_ac=25`, `cmax=0.02`) is identical on every
scalar EXCEPT the two removal numbers, which HEAD reported as `null` because the
same call passed `snow_removal=False`. That is delta 7, visible.

### (c) Live `user_gated` card drives - BOTH templates

The kickoff asks for at least two of the tranche to be driven live through the
harness. The tranche is two, so both were.

**`scripts/drive_rdii_rtk_cards.py`, exit 0.**

- The FORM CARD fired with **15 rows**, titled "Review the RTK unit-hydrograph
  RDII scenario". Every row that was a bare signature default at HEAD now carries
  its declared bounds and a `labeled default` badge - `R1` at `0.1` with bounds
  `[0.0, 1.0]`, `K1` at `2.0` with `[0.0, 20.0]`, and so on. Exactly ONE row
  carried `advanced=True` (`dt_min`), which is the right fold for this template:
  everything else here IS the question.
- **The edit reached the physics, provably, through the RTK volume identity.**
  The driver revised `R1` 0.10 -> 0.20, which raises `sum(R)` from 0.19 to 0.29.
  The chart the RUN ITSELF emitted narrates `RTK RDII (sum R=0.29) over 100 ac,
  1.00 in storm: peak RDII 6.39 cfs, 18% of the node peak vs direct runoff.
  Volume identity ratio 1.0001; native SWMM peak ratio 0.9936.` Against the
  reference run's `sum R=0.19`, peak `3.2951` and 9.9% share. The RDII VOLUME
  ratio is **1.5263 against the predicted 0.29/0.19 = 1.5263** - the identity
  holds under the edit, which is the strongest available statement that the
  approved sheet is what the closed form ran on. The native cross-check ratio
  stayed at 0.994, so the ENGINE agreed with the edited closed form too.
- No draw card, no plain warning: this template declares neither, exactly as its
  plan says.
- The harness reports `no published PRIMARY raster to locate the run prefix` -
  the honest N3 sentence for a chart-first template, not a fabricated prefix.
- Evidence: `docs/proof/swmm_rdii_rtk_cards_evidence.json`.

**`scripts/drive_snowmelt_cards.py`, exit 0.**

- The FORM CARD fired with **37 rows**, titled "Review the rain-on-snow snowmelt
  scenario", **twelve** of them behind the advanced fold (`ati_weight`,
  `depth_at_full_cover_in`, `dt_min`, `free_water_fraction`, the four Horton
  parameters, `initial_free_water_in`, `longitude_correction_min`,
  `negative_melt_ratio`, `plow_out_fraction`). Every one of those twelve was an
  invisible literal inside an f-string at HEAD, and the forcing rows above the
  fold - `cold_temp_f`, `warm_temp_f`, `snowfall_intensity_in_hr`,
  `rain_intensity_in_hr`, `dividing_temp_f` - were a baked demo FUNCTION.
- **The edit reached the physics.** The driver revised
  `snowfall_intensity_in_hr` 0.05 -> 0.10 in/hr. The SWE chart the run itself
  emitted narrates `peak SWE 2.40 in, total melt 1.82 in; plowing cuts peak SWE
  to 0.79 in` against the reference run's `1.20 in` / `1.20 in` / `0.502 in`.
  Doubling the snowfall through the cold spell EXACTLY doubles the peak snow
  water equivalent, which is what a conservation-of-mass answer must do; the melt
  rises sub-proportionally (1.20 -> 1.82 in) because the warm window is unchanged
  and cannot melt twice the pack, and the plowed pack falls because the same
  removal threshold now bites a deeper pack more often. All three responses are
  correct and none is a proportional echo of the edit.
- BOTH declared charts crossed the wire (`snow water equivalent...`,
  `runoff hydrograph...`).
- Evidence: `docs/proof/swmm_snowmelt_cards_evidence.json`.

Both evidence files land under `docs/proof/` (gitignored, like every other proof
JSON there); what is COMMITTED is the drive that writes them.

### (d) Net LOC

| | before | after | delta |
|---|---|---|---|
| `rdii_rtk/rdii_rtk.py` | 461 | 310 | **-151** |
| `rdii_rtk/steps.py` (new) | 0 | 427 | +427 |
| **RDII total** | **461** | **737** | **+276** |
| `snowmelt_degree_day/snowmelt_degree_day.py` | 512 | 486 | **-26** |
| `snowmelt_degree_day/steps.py` (new) | 0 | 459 | +459 |
| **snowmelt total** | **512** | **945** | **+433** |
| `swmm/steps/solve.py` | 95 | 110 | +15 |
| `swmm/steps/series.py` (new) | 0 | 64 | +64 |
| `swmm/steps/charts.py` (new) | 0 | 73 | +73 |
| `swmm/steps/__init__.py` | 31 | 35 | +4 |
| `aquifer_baseflow/steps.py` (repointed) | 371 | 353 | **-18** |
| **shared family** | **497** | **635** | **+138** |
| `scripts/demo_swmm_rdii_epa_table_7_1.py` (new) | 0 | 88 | +88 |
| `scripts/` (2 new card drives) | 0 | 217 | +217 |
| `scripts/` (2 proofs repointed) | 306 | 316 | +10 |
| `tests/` (1 repointed + 1 new) | 123 | 369 | +246 |
| **everything touched** | **1,899** | **3,307** | **+1,408** |

**The SWMM family running total, against the baseline the net-LOC law measures:**

| | `workflows/swmm/` | templates migrated | delta vs baseline |
|---|---|---|---|
| baseline (`b7e898f3`, pre-declarative) | **7,723** | 0 of 15 | - |
| after the checkpoint (ADR 0306) | 8,280 | 1 of 15 | +557 |
| **after wave A (this)** | **9,127** | **3 of 15** | **+1,404** |

Read it straight, the same way ADR 0306 did:

- **The family is net-POSITIVE and will stay so until the giants land.** Three of
  fifteen templates are migrated and the shared family has been paid for twice
  over - once at the checkpoint, once here. The TELEMAC precedent is the honest
  comparison: ADR 0303's wave was +1,613 and the family did not reach
  net-negative until ADR 0305.
- **The template files themselves shrank** (-151 and -26) while gaining bounds,
  doors, editable rows, generated docstrings and a form gate for 52 values that
  were previously unreachable literals or bare signature defaults. `rdii_rtk.py`
  lost a third of its length outright.
- **The shared family started paying back.** `aquifer_baseflow/steps.py` is 18
  lines SHORTER than at the checkpoint for the same behavior, purely from being
  repointed onto `peak` / `coerce_series` / `timeseries_block` /
  `line_chart_spec`. That is a small number and it is the right kind of number:
  it is the first one that went the other way.
- **Where the rest of the return lives, stated so it can be checked.** Waves B
  and C delete two shared composers (393 + 163) and re-declare eight thin
  templates over the step family that already exists. Wave D is where the 2,725
  lines of AOI giants meet a family that already owns the solve loop, the chart
  spec, the deck helpers and the form gate.

### Coded tools

No change. Both templates keep their single registry entries and their co-located
`corpus.yaml` files unchanged. No tool added, none removed.

Both docstrings are now GENERATED (`render_docstring`), which rewrites the
routing view the retrieval pool indexes - so retrieval was re-checked rather than
assumed, model-free, against each template's own corpus queries:
`retrieve_visible_tools(q, None, 8)` returns the template for **10 of 10** queries
on `swmm_rdii_rtk_unit_hydrograph` and **10 of 10** on
`swmm_snowmelt_degree_day`. The generated routing front is budget-enforced at
import, so a routing block that outgrew Bedrock's 1000-char truncation would fail
the module import rather than ship a tool the model cannot route to.

### (e) Parity spot-checks - the four already-migrated workflows

- **`swmm_aquifer_baseflow_to_node`** (Ames, Iowa; the ADR 0306 reference): peak
  node inflow **2.30071 cfs @ 8.25 h**, no-GW peak **1.50835**, between-storms
  baseflow **0.60124** vs **0.0**, contribution **0.60124**, recession tau
  **703.15 h**, storm-2 bump **1.49741**, continuity **0.0%**, soil column
  **0.4637 / 0.1963 / 0.3568 / 0.1318**. BIT-IDENTICAL to ADR 0306, re-run AFTER
  the shared-helper repoint - which is what makes the repoint safe rather than
  hoped-for.
- **`modflow_regional_water_budget`** (Ames, Iowa): `chd_in`
  **9.887537099091208**, `chd_out` **-9.88753734666957**, `aquifer_k_ms`
  **9.298176e-07**, `porosity` **0.156917**, layer bbox
  `(-93.62921267786774, 42.01767770074262, -93.60465753416545, 42.03582715478343)`.
  BIT-IDENTICAL to ADR 0306.
- **`telemac_do_sag`**: `scripts/run_do_sag_direct.py` re-run at this commit.
- **`telemac_river_dye`**: untouched. `tests/test_run_river_dye_scenario.py` green
  in the `[p-r]` slice.

### Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1654 passed, 5 skipped, **0 failed** |
| `tests/test_[f-o]*.py` | 6664 passed, 3 skipped, 1 xfailed, **4 failed** - the baseline `test_fetch_resolution_gate` four, nothing else |
| `tests/test_[p-r]*.py` | 2122 passed, 2 skipped, **0 failed** |
| `tests/test_[s-z]*.py` | 1436 passed, 6 skipped, **0 failed** |
| `contracts/tests` | 729 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True` |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published (`01M0T0C6XTYCY6AYDDK21R7F05`) |
| `scripts/drive_rdii_rtk_cards.py` | exit 0; sum R 0.29 on the run's own chart, volume ratio 1.5263 |
| `scripts/drive_snowmelt_cards.py` | exit 0; peak SWE 2.40 in at the revised snowfall |

`[s-z]` is +16 against ADR 0306's corrected 1420 - the 16 new snowmelt tests. The
other three slices are unchanged, which is what a migration that moves no number
should look like.

No `workers/` path changed: both templates are host-exec (pyswmm in-process), so
no image rebuild was needed and none was done.

Additional proof runs, both green and both regenerating their committed
deliverables unchanged:

- `scripts/proof_swmm_rdii.py` - repointed onto ONE declared invocation of the
  tool (it re-implemented the method beside it before). Reproduces closed-form
  peak **1.0599 cfs**, native SWMM peak **1.0534**, ratio **0.9938**, volume
  identity **1.00006**, RDII fraction **0.306** - the exact numbers in the
  committed panel's own caption.
- `scripts/proof_swmm_snowmelt.py` - likewise repointed onto one declared
  invocation, driven by REAL hourly KBUF ASOS temperature. Reproduces peak SWE
  **4.320 in**, total melt **1.214 in**, plowed peak SWE **1.409 in**, snowmelt
  peak **4.796 cfs**, rain-only **4.034 cfs**, amplification **1.189** - matching
  the physics line recorded in `scripts/seed_showcase_cases.py`. Its five physics
  assertions pass.

New offline coverage: `tests/test_swmm_snowmelt_degree_day.py` (16 tests, new) and
`tests/test_swmm_rdii_rtk.py` (14 tests, +6) - the declared forcing, both deck
variants, the plan shapes, the structural law-9 check per template, the refusals
that replaced the swallows, the bounds clamp, and the physics (a pack forms and
melts; rain-on-snow amplifies; plowing reduces the pack; a dividing temperature
below the cold spell builds no pack at all).

## Consequences

- Three of fifteen SWMM templates are declared. The remaining twelve are two
  families and three giants, and the wave boundaries above follow the shared
  machinery rather than the file sizes.
- The shared step family now owns the solve loop (multi-attribute), the chart
  spec and the deck time-series helpers. Its first repayment is visible: the
  checkpoint's own template got shorter without changing a number.
- Two swallow classes are gone from this engine (a malformed hyetograph reverting
  to the demo forcing; a failed cross-check reporting `null` under
  `status="ok"`), and 52 previously invisible values are on a form card with
  bounds.
- No fork is open in this wave. The Green-Ampt trio (ADR 0306 answer #10) is
  untouched and still NATE's; it does not reach these two templates, and the
  reason is recorded above rather than assumed.
