# ADR 0305 - The river_dye migration + the live-run harness (declarative wave 3)

Status: LANDED (wave 3 of the declarative campaign)
Design: `docs/design/declarative-workflows.md` (migration-order item 3)
Builds on: ADR 0303 (library v1 + do_sag), ADR 0304 (the form and draw cards)

## Context

`telemac_river_dye` was the campaign's full-contact proof: 3,469 lines in one
file, holding a 340-line argument-hardening prologue, a 220-line hand-written
LLM docstring, four substance-class routing tables, the mesh autoscaler, the
whole reach pipeline, a second copy of that pipeline for the approve-mesh
preview, and the do_sag branch bolted through the middle of it. It is also the
template the do_sag migration DELEGATED to, so wave 1 could only declare
`DATA = ()` and call the whole thing one composite step.

This wave declares it: `PARAMS` + `DATA` + a pure `plan(p, d)` over a SHARED
TELEMAC step family, and it is where the form card gets its first live proof
(ADR 0304 shipped the card with no workflow that declares a `FormGate`).

## Decision

### 1. `workflows/telemac/steps/` - the shared reach family

The reach pipeline is no longer river_dye's private body. It is nine modules
every TELEMAC river template can declare against:

| module | lines | what it owns |
|---|---|---|
| `errors.py` | 118 | the four typed failures, two of them retryable GATES carrying `.suggestions` |
| `substance.py` | 266 | the four substance classes, their keyword vocabularies, the module-arming decision |
| `reach.py` | 464 | `Geocode.reach` (rebinds the domain), the `rivers` producer, `ReachSeed`, the mesh autoscaler |
| `forcing.py` | 268 | the rain producer (declared ladder) and the `CarrierDischarge` step |
| `deck.py` | 410 | `WriteDeck.telemac` - the ReachConfig serialization hook + manifest staging |
| `solve.py` | 240 | `Solve.telemac` - dispatch, wait, and the worker's typed gates |
| `products.py` | 474 | `Products.dye` + `publish_do_products` + the chart builder |
| `mesh_preview.py` | 263 | the approve-mesh preview, now CALLING the reach front instead of mirroring it |
| `__init__.py` | 60 | the public surface |
| **total** | **2,563** | |

The template-method shape the design doc names is now structural: `WriteDeck`
has one hook per engine, `Geocode` / `ReachSeed` / `Solve` / `Products` are the
shared skeleton, and a second template (do_sag) already composes them.

The single biggest correctness win is the preview. `preview_telemac_mesh`
carried a MUST-MATCH NOTE - "the seed derivation below intentionally mirrors
Stages 1-2 of the composer... if you change the seed logic THERE, change it
HERE." It now calls `geocode_reach` / `fetch_reach_flowline` / `reach_seed`
directly, so there is one seed derivation and the drift it warned about is
impossible rather than merely documented.

### 2. The plan puts BOTH gates in front of every step

```
FormGate -> DrawGate(release_coords) -> reach -> seed -> carrier_discharge
         -> deck -> solve (consequential) -> plume [chart dye_concentration]
```

Gates first, deliberately. `_eager_data_index` already fires the independent
`Data` batch after the last gate, for the reason wave 1b gave: a producer that
ran earlier fetched against the very params the gate exists to change. The same
argument applies to STEPS, and river_dye has five of them ahead of the solve. A
plan that geocoded before its form card would leave the run's AOI resolved from
a value the user then revised - the contradiction the review exists to prevent.
So nothing consumes a revisable value until both cards are answered.

The plan uses NO `When`. Every routing decision (which substance class, which
GAIA modules, which extra layers) is made at RUN time inside the step that owns
it, from the late-bound `ParamRef`s. That is not an accident of style: the
validator refuses a plan that declares a `FormGate` and also branches on a
value the gate can revise, because a `When` is decided when the plan value is
built. A linear plan is what makes `substance` and `erodible_bed` genuinely
editable on the form.

### 3. `Data` is what the reach FETCHES; the discharge is a step

`DATA` declares two reference producers - `rivers` (the NHD flowline for the
domain) and `rain` (the net rain-or-evaporation forcing, with its ladder
declared as `.ladder("gridmet_domain_mean", "user_rate")`). Both are
`ReferenceProducer`: canonical world data, fetched fresh for the domain, no
`.byo()`.

The carrier discharge is a STEP, not `Data`, and the reason is a validator rule
rather than taste: a producer's `Ref`s may name a param or another `Data`, never
a step, and the NWM lookup needs the resolved mid-reach seed. Declaring the seed
as `Data` to work around that would call a derived point an artifact.

Two fetches are NOT declared, stated plainly:

- **The bed terrain.** `fetch_dem_bed` runs INSIDE the worker container; the
  composer never sees a URI. It is surfaced through the worker-envelope seam as
  a `role="context"` layer (`_surface_bed_bathymetry_input`), which is the honest
  path and the one the ADR 0244 sweep allow-lists. Declaring `Data("terrain")`
  for something the plan does not fetch would be the same lie wave 1 refused to
  tell about do_sag.
- **The geocode.** It is the step that BINDS the domain, and a producer cannot
  rebind it.

### 4. `plan(p, d)` takes `d`, and ignores it

The design doc sketches `d.terrain`; the library has no such view, and
`Ref("rivers")` is the implemented spelling (do_sag passes `d=None`). The
signature is kept so the shape matches the doc and a future `d` view is a
non-breaking addition, but this plan reads its Data through `Ref`.

### 5. do_sag composes the shared steps directly

`ReachSolve.telemac_waqtel_o2` delegated its whole pipeline to
`model_telemac_river_dye`, which this wave deletes. It now composes the shared
steps itself - geocode, flowline, seed, carrier discharge, ITS OWN input review,
deck, solve, DO products.

The alternative was to call the new plan-backed `telemac_river_dye` tool, i.e.
nest one interpretation inside another: two ledgers, two invocation keys, two
law-9 floors, and a form card for a workflow the user did not invoke. Composing
the steps keeps do_sag's behavior identical (its parity reference stands, below)
and keeps `self_gating=True` truthful - the review it runs is over the carrier
discharge and bank source IT resolved, values no plan-level form could show
because they do not exist until the fetch has run.

do_sag's own migration to a declared plan is NOT in this wave. It would have to
give up that self-gating review to gain a form card, and its parity reference is
what this wave is measured against.

### 6. The live-run harness - `trid3nt_server/testing/`

A live test is now a DECLARATION: the tool, its args, the answers its gates get,
and the assertions the run has to satisfy.

```python
RUN = LiveRun(
    tool="telemac_river_dye",
    args={...,"input_mode": "user_gated"},
    case_title="proof: telemac river dye",
    answers=GateAnswers(draw=RELEASE_LONLAT, form_edits={"dye_concentration_mgl": 250.0},
                        require_draw=True, require_form=True),
)
ev = run_live(RUN)
ev.require_ok().require_run_products()
ev.require_layer(name_contains="release", role="context")
```

| module | lines | what it owns |
|---|---|---|
| `ws_client.py` | 111 | the protocol primitives: envelopes, handshake, cases, tool status |
| `live_run.py` | 349 | `LiveRun` / `GateAnswers` / `RunEvidence`, the turn pump, the gate answering, the run-prefix read-back |
| `__init__.py` | 27 | the public surface |

Three properties are load-bearing:

- **A declared answer is also an EXPECTATION.** `require_draw` / `require_form`
  turn a card that never fired into a failure. ADR 0304's live run answered a
  plain review and reported `form_card_rows=0`; a harness that let that pass
  silently would let the next one claim a proof it did not have.
- **The evidence is the run's OWN products.** `RunEvidence` locates the run
  prefix from the published raster and reads `chart_spec.json` + `metrics.json`
  back off it. An assertion cites the product; it never recomputes the answer.
- **It lives in the server package, not the test tree.** Drivers are product
  code (house norm), and `scripts/` importing from `tests/` would invert the
  dependency. `seed_showcase_cases.py` now imports the protocol primitives from
  here rather than defining its own copy, so there is ONE implementation of the
  wire shapes.

It is offline-covered in `tests/test_live_run_harness.py` (13 tests): the point
and multi-vertex answer shapes, the decline, submit-is-the-approval, the
back-compatible sheet-less path, the blocking-event report, and every refusal.

## Inventory - every old behavior re-homed or documented-deleted

The tool's 340-line argument prologue is the bulk of it: 24 of these rows are an
inline `try/float/clamp` or an allow-list that a declared `Param` now states
once, in a form the docstring, the form card and the provenance row all read.

| # | old behavior (`river_dye.py` @ HEAD) | re-homing |
|---|---|---|
| 1 | `coerce_bbox_value` on `bbox` | `_normalize`, unchanged |
| 2 | alpha-string bbox salvaged into `location` | `_normalize`, unchanged |
| 3 | `TELEMAC_PARAMS_INVALID` on an uncoercible bbox | `_normalize`, byte-identical envelope |
| 4 | `TELEMAC_PARAMS_INCOMPLETE` when neither AOI is given | `_normalize`, byte-identical envelope |
| 5 | LOCATION wins when both are supplied | `_normalize`, unchanged |
| 6 | `river_geometry_uri` must be an object URI, else dropped | moved to `fetch_reach_flowline` - the producer that reads it. Param `river_geometry_uri`, door=USER, optional |
| 7 | `spill_location_latlon` "lat,lon" parsed when the split coords are absent | `_release_point` in `_normalize`. CHANGED: an unparseable string now REFUSES typed instead of being logged and ignored |
| 8 | `plausible_release_coords(release_lon, release_lat)`, implausible -> dropped with a warning | `coerce_lonlat_point` in `steps/reach.py`, and it REFUSES. Param `release_coords`, door=USER, optional, `user_lever`, with a `DrawGate`. `plausible_release_coords` DELETED (grep-to-zero) |
| 9 | `compute_class` allow-list coercion to `medium` | `_normalize`, unchanged; Param `compute_class`, door=CONSTANT |
| 10 | `_clamp_domain_extent(reach_length_km, valid 0.5-15, clamp 0.5-8)` + a labeled note | Param bounds `(0.5, 15.0)`. CHANGED: one window, not a valid window plus a different clamp window - see delta 2 |
| 11 | `spill_fraction` clamp `[0.05, 0.9]` (source strictly interior) | Param bounds `(0.05, 0.9)`, door=SCENARIO default 0.25 |
| 12 | `_clamp_domain_extent(sim_duration_s, 600-14400)` | Param bounds `(600.0, 14400.0)`, door=SCENARIO default 3600 |
| 13 | `_clamp_domain_extent(channel_width_m, 10-1500)` | Param bounds `(10.0, 1500.0)`, door=CONSTANT default 60 |
| 14 | `source_q_m3s` clamp `[0.5, 30]` | Param bounds `(0.5, 30.0)`, door=SCENARIO default 8 |
| 15 | `dye_concentration_mgl`, no clamp at all | Param bounds `(0.0, 1e6)`, door=SCENARIO default 100. A permissive bound, declared so a non-numeric value refuses |
| 16 | `spill_duration_s`, no clamp at all | Param bounds `(1.0, 86400.0)`, door=SCENARIO default 300 |
| 17 | `_pos_float(decay_half_life_hours, 0.1, 720)` | Param bounds `(0.1, 720.0)`, door=USER, optional |
| 18 | `_pos_float(decay_rate_per_day, 0.01, 100)` | Param bounds `(0.01, 100.0)`, door=USER, optional |
| 19 | `_pos_float(grain_size_um, 5, 2000)` | Param bounds `(5.0, 2000.0)`, door=USER, optional |
| 20 | `sediment_type` sanitize to 8 alnum chars | `sanitize_substance(limit=8)` inside `resolve_grain`; Param `sediment_type`, door=USER, optional |
| 21 | `_pos_float(bed_thickness_m, 0.05, 50)` | Param bounds `(0.05, 50.0)` |
| 22 | `_pos_float(morphological_factor, 1, 100)` | Param bounds `(1.0, 100.0)` |
| 23 | `bedload_formula` int-coerced, `{1,2,7}` else the default | `deck._sediment_block` (`_BEDLOAD_FORMULAE`) - a SET, not a range, so it is not a declared bound. It is a `numerical`-consequence Param and the deck drops an out-of-set pick with a warning |
| 24 | `friction_law` int-coerced, `{2,3,4}` else None | `deck._resolved_physics` (`_FRICTION_LAWS`), same reasoning |
| 25 | `dredge_mode` allow-list `{scheduled, criterion}` | `deck._sediment_block` (`_DREDGE_MODES`); Param door=CONSTANT default `scheduled` |
| 26 | `_pos_float` on `dredge_volume_m3` / `dredge_crit_depth_m` / `dredge_dig_depth_m` | Param bounds `(1, 1e7)` / `(0.01, 20)` / `(0.05, 30)` |
| 27 | `dredge_disposal` bool coercion | Param door=USER, optional, with `derived_when_absent` naming dredge-only |
| 28 | `_pos_float` on `friction_coefficient` / `velocity_diffusivity` / `tracer_diffusivity` | Param bounds `(10, 90)` / `(1e-3, 10)` / `(1e-3, 10)` |
| 29 | `wind_speed_mps` clamp `[0, 60]` | Param bounds `(0.0, 60.0)`, door=SCENARIO default 0 |
| 30 | `wind_direction_deg % 360` | `_normalize` (a bearing WRAPS, it does not clamp) + Param bounds `(0.0, 360.0)` for the type refusal |
| 31 | `substance` sanitize (alnum, 24 chars, default `dye`) | `steps.substance.sanitize_substance`; Param `substance`, door=QUESTION default `dye` |
| 32 | `contaminant` promotes a tracer-class `substance` to its own non-tracer class | `_normalize`, unchanged. `contaminant` stays a wire kwarg and never becomes a Param - it is a second spelling of one value, resolved before any door |
| 33 | `_scour_hint` auto-arms `erodible_bed` | `steps.substance.arm_sediment_modules`; Param `erodible_bed`, door=USER, optional, `derived_when_absent` naming the auto-arm |
| 34 | `_grad_hint` + `resolve_gradation` + the `graded_sand` default, forcing `erodible_bed` | `arm_sediment_modules`, unchanged |
| 35 | `_dredge_hint` auto-arms `dredging`, forcing `erodible_bed` | `arm_sediment_modules`, unchanged |
| 36 | the tool's `logger.info` call line | replaced by the deck step's granularity log + the tool's completion log, which also carry `executed=` / `replayed=` |
| 37 | the 220-line hand-written LLM docstring | GENERATED by `render_docstring` from 43 Param descs + a 55-line `_DOC`. Routing block measured at 995 chars, inside the 1000-char truncation budget - and the budget is now ENFORCED at import, so a future edit that overruns it fails loudly instead of being silently truncated by the provider |
| 38 | `TEMPLATE_CARD` | kept, plus `release_coords` in the knobs list |
| 39 | `ResolutionSpec` / `AtomicToolMetadata` / `GateSpec` | kept verbatim |
| 40 | `except (TelemacBanksUnavailableError, TelemacReachDegenerateError): raise` | the general rule from wave 1b: an exception declaring `retryable` PROPAGATES. The interpreter re-raises it and the tool body re-raises ahead of its catch-all, so `.suggestions` still reach the adapter |
| 41 | `except (TelemacDyeScenarioError, PostprocessTelemacError)` -> error dict | `StepFailedError` carries the engine's own `error_code`, so the envelope is preserved |
| 42 | `except Exception` -> `TELEMAC_INTERNAL_ERROR` | kept in the tool body |
| 43 | `asyncio.CancelledError` re-raised | kept, in both the tool body and the interpreter |
| 44 | composer: exactly one of location/bbox | `geocode_reach` refuses `TELEMAC_DYE_SCENARIO_INPUT_INVALID` when it has neither; the tool refuses earlier and more specifically |
| 45 | `begin_substeps(_planned)` + five hand-placed `substep(...)` brackets | DELETED. The interpreter owns substep accounting (one node, one substep); law 8 - hand-wired emission in a composer is a defect |
| 46 | `emitter = pipeline_emitter or current_emitter()` and the `pipeline_emitter` parameter | DELETED. Nothing ever passed it, so `_maybe_emit` always ran DIRECT - a dead seam. Steps read `current_emitter()` |
| 47 | `_maybe_emit` | DELETED with it (grep-to-zero) |
| 48 | geocode + whole-state-snap rejection + locality-tail retry + typed ambiguity | `steps.reach._geocode_seed_center`, moved verbatim |
| 49 | bbox-centre AOI + the `AOI (lat, lon)` name | `steps.reach.geocode_reach`, unchanged |
| 50 | `river_bbox = bbox_around(centre, 0.06)` | `geocode_reach` returns it as the step's `bbox`, and `.overrides_domain()` makes it the DOMAIN every producer reads |
| 51 | rain/evap resolution, gridMET window superseding an explicit rate, net clamped `[-50, 2000]` | `Data("rain")` + `forcing.resolve_rain_forcing`, with the precedence now DECLARED as `.ladder("gridmet_domain_mean", "user_rate")`. A gridMET failure still REFUSES typed - degrading a requested real storm to zero rain would be a silent no-rain solve, so "no forcing" is not a rung |
| 52 | prefetched river URI, else `fetch_river_geometry(bbox=river_bbox)` | `Data("rivers")` + `fetch_reach_flowline`, reading the domain |
| 53 | `_river_seed_from_geometry` -> mid-reach seed, else the geocoded centroid, with `seed_source` | `ReachSeed` / `steps.reach.reach_seed`, unchanged |
| 54 | NWM carrier discharge, explicit short-circuit, `TELEMAC_DISCHARGE_INPUT_REQUIRED` on a miss | `CarrierDischarge` / `forcing.resolve_carrier_discharge`, unchanged; Param `discharge_m3s`, door=USER, optional, `consequence="physics"`, `derived_when_absent` naming the NWM resolution |
| 55 | `gate_input_review` over the RESOLVED discharge + bank source | MOVED - see delta 1. river_dye reviews its declared sheet at the `FormGate`; do_sag keeps the resolved-value review it always had |
| 56 | `USER_INPUT_CANCELLED` on a cancelled review | the `FormGate` refuses `INPUT_REVIEW_CANCELLED` (or `PHYSICS_INPUT_REQUIRED` for a law-9 cancel) - the library's own codes, which callers already route on |
| 57 | `suggest_mesh_size_m` + `suggest_time_step_s` + the granularity log | `steps.reach`, moved verbatim; called by `write_reach_deck` AND by the preview, from one definition |
| 58 | `reach_name = _slug(location_name)` | `steps.reach.slug`, on the geocode result |
| 59 | `river_name = _named_watercourse(...)` | `steps.reach.named_watercourse`, on the geocode result |
| 60 | `classify_substance` + the four preset tables + the keyword vocabularies | `steps/substance.py`, moved verbatim |
| 61 | erodible-bed forces the sediment class + the `assert` | `deck._substance_block`. The `assert` is replaced by CONSTRUCTION: the block that sets `erodible_bed` is the same one that sets `substance_class`, so there is no window in which they can disagree. Pinned by four tests |
| 62 | decay law/coefficient resolution from the preset + overrides | `substance.resolve_decay_law` |
| 63 | sediment type/d50 resolution + `[5, 2000]` clamp | `substance.resolve_grain` |
| 64 | the `_release_seeds_reach` / `_seed_release_lon` / `_seed_release_lat` TRI-STATE | COLLAPSED into one declared Param, `reach_seed_coords`. `_normalize` resolves the tri-state before any door: an explicit seed pair wins, a gate-picked click (`_release_seeds_reach is False`) leaves it absent, otherwise the release point seeds the reach. The three private kwargs remain on the signature because the approve-mesh decision tail writes them |
| 65 | `publish_release_point` (Outfall / Release point label) | `write_reach_deck`, unchanged - it is the step that knows which point was used and whether the user placed it |
| 66 | `validate_and_resolve_physics` over only the keys the user set | `deck._resolved_physics`, unchanged |
| 67 | the `reach` dict assembly with its conditional blocks | `deck.write_reach_deck` + `_substance_block` / `_sediment_block` / `_do_sag_block`. Every optional block still rides ONLY when asked for, so an unused module leaves the deck byte-identical |
| 68 | `_stage_manifest` (+ the sediment output list, + `mesh_only`) | `deck.stage_manifest`, moved verbatim |
| 69 | `run_solver` + sim cards + progress task + `route_sim_terminal` + the wait bound | `steps.solve.solve_reach`, moved verbatim |
| 70 | solve failure -> read metrics -> banks / degenerate gates -> `TELEMAC_DYE_RUN_FAILED` | `solve_reach`, unchanged |
| 71 | `_download_telemac_result` (SELAFIN + `utm_epsg`, typed on a miss) | `solve.download_result_selafin`. The solve step no longer RETURNS the local path - a replayed ledger record must not hand back a temp file a later process cannot see, so the products step re-downloads from the prefix the replay probe just confirmed |
| 72 | `_read_run_metrics` -> `bank_provenance` | `solve.read_run_metrics`; the provenance rides out on the solve result |
| 73 | `_surface_bed_bathymetry_input` (the worker-envelope bed COG) | `steps/products.py`, moved verbatim, name kept so the ADR 0244 sweep still sees it. Allow-list re-pointed |
| 74 | the do_sag early return -> `_postprocess_and_publish_do_sag` | `products.publish_do_products`, called by do_sag's own composition. river_dye no longer carries a do_sag branch at all |
| 75 | `postprocess_telemac` + the SELAFIN unlink | `products.publish_dye_products`, unchanged |
| 76 | `TELEMAC_DYE_NO_LAYERS` on an empty tracer field | unchanged |
| 77 | the two `SyntheticInput` provenance rows (discharge + bank geometry) | `products._provenance`, unchanged; merged with the plan's declared rows through `merge_provenance` (the step's row wins on a collision) |
| 78 | the `domain_extent_clamped` provenance row fed by `domain_clamp_notes` | DELETED, re-homed STRUCTURALLY: the resolver stamps `CLAMPED from X to the declared maximum Y` on the row of the param that actually clamped, so the transparency is per-value instead of one lumped string, and it costs no threading |
| 79 | `_publish_peak_layer` (honesty note, mesh meta, synthetic inputs, publish-failure passthrough) | `products._publish_peak_layer`, moved verbatim |
| 80 | `publish_results_mesh_via_seam` (outputs.json + the SELAFIN mesh layer) | `publish_dye_products`, unchanged |
| 81 | the sediment fold: GAIA download, NET bed mass clamp, deposition postprocess, `max_scour_mm`, publish | `products._fold_sediment_products`, moved verbatim |
| 82 | the oil slick: upload-before-register HEAD guard, then emit | `products._emit_oil_slick`, moved verbatim |
| 83 | `_maybe_emit_chart` (the two honest rise-to-peak points) | `products.build_dye_chart`, declared as `.chart("dye_concentration", ...)`. The SPEC is now the product: it rides out on `RunResult.charts` and is persisted to the run prefix |
| 84 | the authoritative last `zoom-to` | `publish_dye_products`, unchanged |
| 85 | `preview_telemac_mesh` - its own geocode / river-fetch / seed mirror | `steps/mesh_preview.py`, now CALLING the shared reach front. The MUST-MATCH NOTE is deleted because the duplication is |
| 86 | the preview's `[0.5, 8.0]` reach clamp | CHANGED to `[0.5, 15.0]`, matching the declared Param bound - see delta 3 |
| 87 | `RunTelemacError` | DELETED. Declared, exported, and never raised anywhere in the repo (grep-to-zero) |
| 88 | `_clamp_domain_extent` | DELETED. Declared bounds are what a clamp is now, and they label themselves |
| 89 | `_pos_float` | DELETED. Bounds + the non-numeric refusal replace it, with one behavior change (delta 4) |
| 90 | `model_telemac_river_dye` (the 870-line composer) | DELETED after acceptance. Its phases are the shared step family |

### Behavior deltas (deliberate, five)

**1. The fetched carrier discharge is no longer reviewed AT ITS VALUE by
river_dye.** This is the one that needs NATE's eye.

The old composer resolved the NWM discharge and then showed it - `discharge_m3s
= 2.1 m3/s [site-derived, NOAA National Water Model]` - for approval before the
solve. In the declarative shape the form card renders the PARAM SHEET, and the
discharge is a `Data`-shaped fetch that runs after the gates by design (wave 1b,
observation 10: a producer must not fetch against the params the gate exists to
change). So the card now shows `discharge_m3s` as an EDITABLE ROW carrying its
`derived_when_absent` text - "resolved from the NOAA National Water Model at the
reach; no NWM coverage refuses typed" - and the user pins a value there instead
of adjusting a fetched one.

What is preserved: the lever (the row is editable and clamped), the provenance
(`basis=fetched` + the real source, stamped on the returned layer), and the
typed refusal when NWM has no coverage. What is lost: seeing the actual number
before approving it.

The fork NATE may want to rule on: keep this, or give the library a way for a
producer to contribute a row to the gate that follows it - which would mean
moving the gate after the producers and giving up the "gates before steps"
property this plan relies on. **do_sag is unaffected** - it keeps the
resolved-value review, which is exactly why its step stays `self_gating`.

**2. `reach_length_km` out-of-range clamps to 15 km, not 8 km.** The old
`_clamp_domain_extent` had a "valid" window `[0.5, 15]` that passed through and
a DIFFERENT clamp window `[0.5, 8]` for anything outside it, so 12 km ran and
50 km became 8 km. A declared bound is one window. `(0.5, 15.0)` is the honest
choice: 12 km is the do_sag reference reach and demonstrably modelable, and the
node budget coarsens the mesh for a long reach anyway.

**3. The approve-mesh preview clamps the reach to the same window.** It clamped
to `[0.5, 8]` while the tool allowed up to 15, so any 8-15 km ask previewed a
DIFFERENT reach than it solved. That was a live inconsistency at HEAD, not
something this wave introduced; it is fixed by both reading the same bound.

**4. A below-range knob CLAMPS instead of being dropped.** `_pos_float` returned
`None` for a non-positive or non-numeric value, which silently reverted the knob
to the deck's own literal. A declared bound clamps to the minimum and SAYS so on
the row; a non-numeric value refuses typed (the wave-1 delta-1 class). Concretely
`friction_coefficient=0` used to become the deck's 33 with no record and now
becomes 10 with `CLAMPED from 0` on its provenance row.

**5. A malformed release point REFUSES.** `plausible_release_coords` dropped an
implausible pair with a warning and ran at `spill_fraction` instead - modelling
a different release location than the one asked for. `coerce_lonlat_point`
refuses `TELEMAC_PARAMS_INVALID`, matching what do_sag's `coerce_outfall_point`
already did. The pre-dispatch gate builder, whose contract is to fail OPEN,
catches the refusal explicitly at its call site and previews without the point;
the tool then refuses a moment later.

## Consequences

### Net LOC - the honest arithmetic

This is the wave the net-LOC law names as the family's judgment point, so here
is the raw truth rather than the flattering slice of it.

| | before | after | delta |
|---|---|---|---|
| `river_dye/river_dye.py` | 3,469 | 671 | **-2,798** |
| `telemac/steps/` (new shared family) | 0 | 2,563 | +2,563 |
| `do_sag/steps.py` | 205 | 273 | +68 |
| `gates/cards/solver_confirm.py` | 1,400 | 1,408 | +8 |
| **TELEMAC family total** | **12,517** | **12,349** | **-168** |
| `trid3nt_server/testing/` (the harness) | 0 | 487 | +487 |
| `scripts/seed_showcase_cases.py` | 1,042 | 967 | -75 |
| `scripts/drive_do_sag_cards.py` | 254 | 90 | -164 |
| `scripts/drive_river_dye_cards.py` | 0 | 105 | +105 |
| `scripts/run_river_dye_direct.py` | 0 | 118 | +118 |
| `scripts/proof_river_dye_frames.py` | 0 | 245 | +245 |
| `tests/test_run_river_dye_scenario.py` | 505 | 562 | +57 |
| `tests/test_live_run_harness.py` | 0 | 210 | +210 |
| tests (repointed) | 966 | 972 | +6 |
| **everything touched** | **7,841** | **8,671** | **+830** |

Read it straight:

- **The engine family is net-NEGATIVE, by 168 lines.** That satisfies the law's
  letter and nothing more. It does not recover wave 1's one-time +1,613 library
  cost, and it will not on its own.
- **The migration itself is roughly a wash.** 2,798 lines left river_dye and
  2,563 arrived in the shared family. What actually DIED is the duplication and
  the swallow machinery: the tri-state, `_pos_float`, `_clamp_domain_extent`,
  `plausible_release_coords`, `_maybe_emit`, the hand-wired substeps, the
  hand-written docstring, and the preview's mirrored reach front. What was ADDED
  is a 200-line `PARAMS` declaration that three surfaces now read (the model's
  docstring, the form card, the provenance rows) where previously each was
  written separately.
- **Counting the harness and the drivers, the wave is +830.** The harness is new
  capability, not migration; saying otherwise would be picking the denominator.

Where the family's real net-negative lives, stated so it can be checked rather
than promised: five TELEMAC templates have NOT been migrated -
`coastal_tidal_surge` (843), `agitation` (835), `stratified_flow` (732),
`rain_on_grid` (680) and `wave_field` (690), 3,780 lines that each re-implement
some part of the geocode -> seed -> manifest -> solve -> publish skeleton this
wave extracted. That is the pool the shared steps exist to collapse, and this
wave is what makes collapsing them a re-declaration rather than a rewrite. The
family's verdict is due when they land, not here.

### Coded tools

No change: `telemac_river_dye` and `telemac_do_sag` keep their single registry
entries. No tool added, none removed.

### The offline baseline changed

`tests/test_run_river_dye_scenario.py` carried TWO known-red tests, part of the
repo's documented 4+2 baseline. Both asserted behavior the code deliberately
changed long before this wave:

- `test_tool_rejects_invalid_bbox` passed `bbox="not,a,bbox"` and expected a
  refusal, but an alpha-string bbox with no `location` has been SALVAGED into
  `location` since the live Twin Falls drive;
- `test_tool_rejects_both_location_and_bbox` expected `PARAMS_INCOMPLETE` for
  both-supplied, but LOCATION has WON since the live Longview drive.

They are rewritten to assert what the code actually does and why, plus a new
`test_numeric_garbage_bbox_refuses_typed` for the case the first one meant to
cover. **The new offline baseline is 4 + 0**: the four `test_fetch_resolution_gate`
failures stand; river_dye contributes none.

## Acceptance evidence

Reference question, both runs: `telemac_river_dye(location="Eel River near
Scotia, California", substance="dye", spill_fraction=0.25, spill_duration_s=300,
dye_concentration_mgl=100, reach_length_km=6, sim_duration_s=3600,
source_q_m3s=8, channel_width_m=60, mesh_resolution="auto",
discharge_m3s=2.2)`. Real NHDPlus reach, real NHDArea banks, local-docker
`trid3nt-local/telemac:latest`.

The carrier discharge is PINNED at 2.2 m3/s in both runs - the value
`fetch_noaa_nwm_streamflow` resolves at this reach today. A value that moves
between two runs is not a parity test. `scripts/run_river_dye_direct.py` carries
the `--discharge-m3s` flag for exactly this, mirroring the do_sag driver.

**(a) Inventory** - the 90-row table above; every behavior re-homed, five
deliberate deltas recorded, six symbols documented-deleted.

**(b) Old vs new** - BIT-IDENTICAL, not merely within tolerance:

| | OLD (`1d104b5d`) | NEW (declarative) |
|---|---|---|
| run id | `01M0S9PP07192HC75WPDPKE6HK` | `01M0SAZ6ED8Y82ZMPT1Q07F8CD` |
| peak concentration | 14.6854887008667 mg/L | 14.6854887008667 mg/L |
| peak arrival time | 140.0 s | 140.0 s |
| plume reach | 3570.1 m | 3570.1 m |
| active frames | 25 | 25 |
| mesh edge / node estimate | 14.0 m / 4271 | 14.0 m / 4271 |
| resolution label | `auto (auto)` | `auto (auto)` |
| layer bbox | `(-124.15819, 40.49583, -124.08739, 40.51634)` | identical |

Byte-identity IS expected here and was worth checking rather than assuming: the
pipeline has no RNG, gmsh is deterministic for a fixed target edge length, and
the only time-varying input (the NWM discharge) is pinned. The tolerance that
would have been honest otherwise is the fourth significant figure, which is what
a 0.1 m3/s discharge difference moved in ADR 0303's do_sag runs.

The new run additionally wrote `chart_spec.json` + `metrics.json` to its own
prefix - products river_dye did not previously persist at all.

**(c) Live end-to-end in `user_gated` mode, driven by the harness** -
`scripts/drive_river_dye_cards.py`, run `01M0SD12B88HDT976HPAXJB8CP`, exit 0.

- **THE FORM CARD FIRED, LIVE, FOR THE FIRST TIME.** 43 rows, titled "Review the
  river-tracer scenario", question-bearing rows first and `door=constant` rows
  carrying `advanced=True` for the fold. Every row arrived with the declaration
  around it: value, units, bounds, door, basis and a server-rendered source badge
  (`you supplied this` / `labeled default` / `not supplied`).
- **The edit reached the physics, provably.** The driver edited exactly one row,
  `dye_concentration_mgl` 100 -> 250, and submitted it as `narrow_scope` +
  `revised_args`. The run's persisted `metrics.json` reports
  **`dye_cmax_mgl = 36.7137`** against the reference run's **14.6855** at 100
  mg/L. The ratio is **2.5000**, which is 250/100 - the source concentration the
  user typed into the card, carried through the re-seat, the deck, the solver and
  the postprocess. `dye_peak_time_s` (140.0), `plume_reach_m` (3570.1) and
  `active_frames` (25) are unchanged, which is what a pure source-strength change
  should do to a linear tracer.
- **The draw card answered the release point.** `mode=point`, title "Draw release
  coords", answered with the USGS Eel River at Scotia gage `[-124.0983, 40.4921]`
  - and `Release point (user) - scotia_...` is on the canvas as a `role="context"`
  vector whose GeoJSON carries `basis: "user"`.
- **The approve-mesh gate ALSO fired** (a sheet-less warning, answered as a plain
  proceed), and its `Mesh preview (14 m edges, 8,778 nodes)` wireframe landed on
  the canvas before the card. The two gates coexist correctly: the server's
  pre-dispatch mesh gate is about the GEOMETRY, the plan's form is about the
  SHEET. Note that the previewed mesh MEASURED 8,778 nodes against the estimate's
  4,271 - the real-bank width is wider than the stated 60 m, which is exactly the
  case the preview's measured-node re-clamp exists for.
- **Six layers on the canvas**: the mesh preview, the fetched river geometry (the
  emit-on-fetch seam), the release point, the in-worker bed bathymetry, the
  results-mesh SELAFIN (the temporal artifact) and the peak concentration raster.
- **The run's own products are in its prefix**: `chart_spec.json` (the
  `dye_concentration` spec, titled "Peak dye concentration - Eel River near
  Scotia, California") and `metrics.json`. One chart emission crossed the wire.

**(d) The proof deliverable** - `scripts/proof_river_dye_frames.py`, rendered
from run `01M0SAZ6ED8Y82ZMPT1Q07F8CD`'s PUBLISHED artifacts:

- `docs/proof/templates/telemac_river_dye_plume_animation.gif` - 26 frames of the
  published `r2d_river.slf` (the temporal artifact the emit-on-solve seam
  registers), on its REAL element connectivity, with the mesh wireframe over the
  field, on ESRI World Imagery. The pulse releases at t=140 s (9.5 mg/L local
  max), travels downstream and dilutes to 1.0 mg/L by t=2800 s.
- `docs/proof/templates/telemac_river_dye_peak_concentration.png` - the published
  `telemac_dye_peak.tif` over the same basemap.

Both are coloured through the product's OWN styling seam - the renderer calls
`publish_layer._resolve_qgis_style_params` and reads back
`rescale=(0.742508, 4.4146) colormap=viridis`, the identical string the publish
log shows. Nothing is re-solved and no palette is invented.

**(e) Net LOC** - the table above.

**(f) do_sag parity** - run `01M0SB3W59ASH740T8WTJJFGRF`, pinned at 2.0 m3/s:

| | ADR 0303 reference | ADR 0305 (steps composed directly) |
|---|---|---|
| DO sag minimum | 8.5772 mg/L | **8.5772 mg/L** |
| sag location | 10631.7 m | **10631.7 m** |
| violates the 5 mg/L standard | false | **false** |
| points / first / last | 60 / 9.022 / 8.9623 | **60 / 9.022 / 8.9623** |

`executed=['do_field', 'do_field.chart:do_sag_curve'] replayed=[] notes=[]`.
Bit-identical: composing the shared steps directly changed no physics.

The do_sag CARD driver, ported onto the harness, is green too
(`01M0SD6Q418XQXZMDZVXJ7WFVE`): the draw card fired and was answered with the
gage point, `Outfall (user) - scotia_...` is on the canvas, `form_card is None`
(do_sag declares no `FormGate`, exactly as ADR 0304 said), and the products are
in the prefix. Its DO minimum is 8.6534 mg/L at 546.1 m against ADR 0304's 8.6537
at 546.1 for the same drawn point - the fourth-digit difference is the UNPINNED
NWM discharge moving between the two days, the same sensitivity ADR 0303 recorded
for 2.0 vs 2.1 m3/s.

## Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1639 passed, 5 skipped, **0 failed** |
| `tests/test_[f-o]*.py` | 6657 passed, 3 skipped, 1 xfailed, **6 failed** - the 4 baseline `test_fetch_resolution_gate` + 2 in `test_model_fire_spread_chain.py` |
| `tests/test_[p-r]*.py` | 2118 passed, 2 skipped, **0 failed** (was 2 baseline - see below) |
| `tests/test_[s-z]*.py` | 1418 passed, 6 skipped, **0 failed** |
| `contracts/tests` | 729 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True`, case self-cleaned |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |

Two baseline notes, both stated rather than absorbed:

- **The [p-r] baseline is now 0.** The two `test_run_river_dye_scenario` failures
  were stale assertions about bbox handling, rewritten to the behavior the code
  actually has (see "The offline baseline changed"). The repo's documented
  baseline is **4 + 0**, not 4 + 2.
- **The two `test_model_fire_spread_chain` failures are NOT this wave's.** They
  were verified by stashing the entire change set and re-running them on clean
  `1d104b5d`, where they fail identically. They are an undocumented pre-existing
  red in [f-o] (both run a real ELMFIRE solve, so they are environment-sensitive
  rather than pure offline tests), and they are flagged here rather than folded
  into the baseline as if they had always been counted.

No `workers/` path was touched, so no image rebuild is in play.

## Stated honestly

- **The `purpose` field on a point draw request reads `barrier`.** The wire
  contract documents `purpose` as vector_draw-only with a `barrier` default, so a
  `mode="point"` request carries it inertly. The plugin's point pick ignores it.
  Not introduced here and not fixed here; recorded because the live evidence shows
  it and the next reader will wonder.
- **The mesh wireframe in the animation is deliberately faint.** At this reach
  scale the 14 m elements are about two pixels, so a heavier line stops reading as
  a mesh and starts reading as a hatch that hides the field it is supposed to
  contextualize. It is drawn OVER the plume (a wireframe under an opaque field
  proves nothing) at `linewidth=0.08, alpha=0.22`, and the caption names it.
- **The QGIS visual pass is NATE's.** The proof renders are matplotlib over ESRI
  imagery with the product's own colormap and rescale; nobody has looked at these
  layers in QGIS, and this report does not claim otherwise.

## Not in this wave

- **do_sag's own plan.** It composes the shared steps imperatively and keeps its
  self-gating review; migrating it to a declared plan means trading that review
  for a form card, which is a NATE call and would move its parity reference.
- **A declared `.render()` on the products step.** The peak COG is published
  INSIDE the step, through the one `publish_layer` chokepoint. Declaring a render
  node would style and publish the same COG a second time and hand the layer back
  with the wrong URI. `.render()` becomes the right home when the render toolset
  promotes the publish seam into declared primitives, exactly as the design doc
  says.
- **`Data("terrain")`.** The bed DEM is fetched inside the worker; see decision 3.
- **The five unmigrated TELEMAC templates**, which is where the family's net-LOC
  verdict actually gets decided.

---

## Correction - wave 3b (adversarial verification)

A verifier REFUTED this landing on three items. They are answered here rather
than by editing the account above, so the record shows what was claimed and what
was actually true.

### B1. The preview clamp was documented, not landed

Row 86, delta 3 and the deletion ledger all said the approve-mesh preview now
clamps `reach_length_km` to `[0.5, 15.0]`. The code still clamped to `[0.5, 8.0]`
(`steps/mesh_preview.py`), so `do_sag`'s default 12 km reach PREVIEWED an 8 km
mesh and SOLVED a 12 km one - the exact drift the row claimed was fixed. The
window is now the declared Param bound, in one line, with the reason on it.

### B2/B3. The release point: the tool refused, the WORKER relocated

Delta 5 said "a malformed release point REFUSES". That was true and too narrow.
`coerce_lonlat_point` refuses a point that is not a (lon, lat) pair on the earth.
A perfectly well-formed point that simply is not ON the meshed reach took a
different path: the worker accept-radius-tests it against the built mesh
(`telemac_river_dye_build.spill_point`, 2 stated channel widths or 1.5x the
widest real bank span) and, on a miss, walks `spill_fraction` instead. It records
the miss - `release_point_used` / `release_point_rejected_dist_m` - but NOTHING
agent-side read those keys. So the run completed, published its plume, and left
`Release point (user)` on the canvas at a point the solve never used.

This is not hypothetical: it is what acceptance run `01M0SD12B88HDT976HPAXJB8CP`
above did. Its `telemac_metrics.json` reads `release_point_used: false,
release_point_rejected_dist_m: 777.3`. The USGS Eel River at Scotia gage sits
777 m off the 6 km meshed reach, and the marker in that evidence is 777 m from
where the plume actually seeded. The card drive was reported as proof that "the
plume starts where the user clicked". It was not.

**What the 2.5000x form-card comparison actually proves.** It stands, and the
reason is uncomfortable: BOTH runs (the 100 mg/L reference and the 250 mg/L card
drive) released at `spill_fraction = 0.25` on the identical mesh, because the
drawn point was rejected in the card run and never supplied in the reference. The
only difference between them was the source concentration, so the ratio is a
clean source-strength test. Had the point been honored, the comparison would have
been confounded by a moved source and the 2.5000x would NOT have appeared. The
number was right; the sentence around it ("the release is a USER value and the
plume starts where the user clicked") was wrong.

**The fix, agent-side only** - no `workers/` path is touched, so no image rebuild
is in play:

1. `release_point_used` / `release_point_rejected_dist_m` join
   `_COMPLETION_METRIC_KEYS`, and `steps/products.py` reads them.
2. `solve_reach` reconciles the verdict the moment the metrics land, BEFORE the
   postprocess: a release point the deck ASKED for and the worker did not honor
   raises the new retryable `TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN`, naming the
   rejected distance, the meshed reach's length and mean width, and three
   corrective retries (a point on the reach, a longer `reach_length_km`, or no
   `release_coords` at all). A user's explicit click silently relocated is the
   law-9 swallow class; refusing is the only honest answer, and the fallback walk
   stays exactly what it always was for a run that asked for no point.
3. The peak layer carries a `release_point` provenance row: the supplied point
   (`basis=user`, "honored by the solver") or the derived `spill_fraction`
   position. A supplied-but-unhonored row is unreachable by construction, because
   the run refuses before products.

**The marker's ordering, stated.** The marker still publishes BEFORE the solve,
in `write_reach_deck`. Deferring it would hide the user's own input for the whole
run, which is the thing it exists to show. What makes that honest is the
reconciliation on the other side: no COMPLETED run can carry a user-placed marker
the plume disagrees with, because the disagreement refuses. A refused run does
leave the marker on the canvas beside a typed error that names the distance - the
input you asked for, and why it could not be used.

**Why the test is not duplicated agent-side.** The accept radius is a property of
the BUILT MESH (the widest real bank span), which the server does not have until
the worker has written its metrics. A second, differently-defined proximity test
in the composer would be a second law, and the two would drift. One test, in the
worker; the verdict reconciled where it lands. The cost is that a rejected point
is learned after the solve rather than before it.

### Acceptance evidence, redone

Both drives are the harness, `scripts/drive_river_dye_cards.py --case
{honored,refused}`, same args as the run above (`dye_concentration_mgl` 100 ->
250 on the form card, `discharge_m3s` pinned at 2.2). Evidence JSONs are
persisted, not left in `/tmp`:
`docs/proof/templates/telemac_river_dye_release_{honored,refused}_evidence.json`.

**(c1) HONORED** - run `01M0SJFVRS1W71S4XDZDH29JW2`, exit 0. The drawn point
`[-124.106759, 40.509617]` is a node ON the meshed Eel reach (read off the
earlier run's `river.slf` and reprojected from UTM 10N, so it is a real point on
the modeled water body, not a guess).

| | |
|---|---|
| draw card | `mode=point`, "Draw release coords", answered with the in-domain point |
| form card | 43 rows, "Review the river-tracer scenario", one row edited |
| `release_point_used` (worker metrics) | **true**, `release_point_rejected_dist_m: null` |
| source the SOLVER wrote (`t2d_river.cas` ABSCISSAE/ORDINATES OF SOURCES) | 406232.496, 4484910.433 UTM 32610 -> `[-124.106759, 40.509616]` |
| drawn point -> actual source | **0.1 m** |
| `dye_cmax_mgl` / `dye_peak_time_s` / `plume_reach_m` / `active_frames` | 42.2232 / 140.0 / 3229.4 / 18 |
| layers | mesh preview, river geometry, `Release point (user)`, bed bathymetry, results-mesh SELAFIN, peak concentration |

The marker and the plume agree by MEASUREMENT: the driver reads the source
coordinate out of the deck the solver actually wrote and compares it to the point
the card was answered with. And the physics moved with the point - 42.2232 mg/L
over 3229.4 m in 18 frames, against the `spill_fraction` fallback's 36.7137 mg/L
over 3570.1 m in 25 frames at the same 250 mg/L source. An honored release is a
different run, which is the whole reason relocating it silently was a defect.

**(c2) REFUSED** - the same drive with the Scotia gage `[-124.0983, 40.4921]`,
exit 0 on the refusal assertions. Both cards fired and were answered; the run
then refused:

```
INTERNAL_ERROR: [TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN] The release point you gave
sits 777 m from the nearest meshed node, so the solver could not put the source
there. The meshed reach is 7019 m long and 211 m wide on average. Nothing was
relocated for you: releasing the substance somewhere else would answer a
different question. Retry with a point ON the modeled reach, with a longer
reach_length_km so the point falls inside it, or without release_coords to
release at spill_fraction along the reach.
```

No run prefix, no products, no peak layer, no chart. The 777 m is the worker's
own measurement, not a recomputation.

Two things this run exposed, both recorded rather than absorbed:

- **The envelope's `error_code` reads `INTERNAL_ERROR`** while the message
  carries `[TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN]`. That is the `dev-tool-invoke`
  path flattening any raised typed exception, and it predates this wave (the
  banks gate surfaces the same way). The conversational path is unaffected - the
  adapter harvests `.suggestions` off the raised exception. Not fixed here.
- **The old `require_ok` would have PASSED this refused run.** Its evidence is
  `dispatched=True, is_error=False, tool_status=None, turn_complete=False`, which
  the harness read as "a typed result carries no status, and that IS success".
  The rider below closes it, and this run is the proof it was needed.

### Riders (verifier non-blockers, all landed)

| # | item |
|---|---|
| 1 | `discharge_m3s` gains declared bounds `(0.01, 1.0e5)` in BOTH templates - the form-card edit was unclamped, and a non-numeric edit died as a generic `STEP_FAILED` instead of the typed bound refusal |
| 2 | The house baseline text is now "EXACTLY 4 fetch_resolution in [f-o] + 0 in [p-r]" (CLAUDE.md law 1), matching what this wave actually left behind |
| 3 | The harness cross-checks `form_edits` against the sheet's ROWS: an edit naming a row the card does not carry is a `LiveRunError` before it is submitted, instead of a silently-ignored revision the test then asserts about |
| 4 | `require_ok` fails a turn that never completed (see the refused run above) |
| 5 | `scripts/ws_smoke.py` (-97 lines) and `scripts/tool_routing_bench.py` (-83) now import the protocol primitives from `trid3nt_server.testing.ws_client`; the last two `mk` copies are gone, and the wire shapes have ONE implementation |
| 6 | `solve_waqtel_o2` - the function wave 3 rewrote - is now offline-covered for REAL: two tests drive the actual composition (order of the eight steps, the outfall riding as `reach_seed_coords`, the saturation clamp, the review's mode, and a cancelled review refusing before the solve) instead of monkeypatching it away |
| 7 | `declarative/docstring.py` states the budget precisely: it guards the PRE-`Returns:` front, not the rendered routing view |

### The five do_sag deltas the inventory did not list

Decision 5 said do_sag's behavior is identical and pointed at its parity run. The
physics is identical - the parity numbers below are bit-identical again - but
"identical" was too strong for the seams around it. Composing the shared steps
instead of delegating to `model_telemac_river_dye` changed five things:

1. **The review names the right tool.** `gate_input_review(tool_name=...)` was
   `telemac_river_dye` (the composer's name, for a run the user started as
   `telemac_do_sag`); it is now `telemac_do_sag`, and so is the
   `USER_INPUT_CANCELLED` message.
2. **`plausible_release_coords` no longer filters the outfall.** The old path ran
   the coerced point through it and silently dropped an implausible one to
   "no outfall"; that symbol is deleted, so `coerce_outfall_point`'s
   on-the-earth check is the whole test - and it REFUSES rather than dropping.
3. **The outfall rides a declared Param.** `release_seeds_reach=True` +
   `seed_release_lon` + `seed_release_lat` (three private kwargs) collapsed into
   `reach_seed_coords`.
4. **The five hand-placed substep brackets are gone.** The interpreter accounts
   the composite as ONE node, so do_sag's progress narration is coarser than the
   composer's was.
5. **do_sag no longer travels through the dye branch at all** - no substance
   classification, no gradation/erodible/decay arming, no rain resolution
   (`rain=None`). The staged deck is unchanged, but nothing on do_sag's path can
   arm a GAIA or WAQTEL-decay module by accident any more.

A sixth, from 3b: the release-point reconciliation runs inside `solve_reach`, so
do_sag inherits it - and it is a no-op there, because do_sag's outfall seeds the
CENTERLINE (`reach_seed_coords`) and never writes `release_lon`/`release_lat`, so
no point is ever "asked for" in the sense the guard tests.

### The fire-spread gate rows were wrong

The gates table blamed two `test_model_fire_spread_chain.py` failures on the
tests running a real ELMFIRE solve and called them environment-sensitive. The
file MOCKS its solves; the stated cause is wrong. Both tests PASS on the
verifier's box and passed on every 3b run of the [f-o] slice (6661 passed, 4
failed - the four `test_fetch_resolution_gate` baseline rows and nothing else).
Whatever failed them on the wave-3 box, it was not an ELMFIRE solve, and no red
outside the documented baseline stands today.

### The net-LOC table put a non-family file in the family total

`gates/cards/solver_confirm.py` is the shared solver-confirmation card, not a
TELEMAC-family file, and it was inside the block that footed to "TELEMAC family
total". Corrected:

| | before | after | delta |
|---|---|---|---|
| `river_dye/river_dye.py` | 3,469 | 671 | -2,798 |
| `telemac/steps/` | 0 | 2,563 | +2,563 |
| `do_sag/steps.py` | 205 | 273 | +68 |
| **TELEMAC family total** | **11,117** | **10,941** | **-176** |
| `gates/cards/solver_confirm.py` (shared, not family) | 1,400 | 1,408 | +8 |

The family is still net-negative, by 176 lines rather than 168, and the
conclusion is unchanged: the verdict is due when the five unmigrated templates
land.

### 3b's own arithmetic

| file | before | after | delta |
|---|---|---|---|
| `steps/errors.py` | 118 | 166 | +48 |
| `steps/solve.py` | 240 | 267 | +27 |
| `steps/products.py` | 474 | 504 | +30 |
| `testing/live_run.py` | 349 | 365 | +16 |
| `scripts/ws_smoke.py` | 380 | 283 | **-97** |
| `scripts/tool_routing_bench.py` | 824 | 741 | **-83** |
| `scripts/drive_river_dye_cards.py` | 105 | 184 | +79 |
| `tests/test_run_river_dye_scenario.py` | 562 | 640 | +78 |
| `tests/test_telemac_do_sag.py` | 215 | 334 | +119 |
| `tests/test_live_run_harness.py` | 210 | 237 | +27 |
| the four one-line/one-comment touches (`docstring.py`, `run_telemac.py`, `deck.py`, `mesh_preview.py`, the two Param bounds, `steps/__init__.py`) | | | +17 |
| **3b total** | | | **+260** |

### Gates (3b)

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1639 passed, 5 skipped, **0 failed** |
| `tests/test_[f-o]*.py` | 6661 passed, 3 skipped, 1 xfailed, **4 failed** - the documented `test_fetch_resolution_gate` baseline, nothing else |
| `tests/test_[p-r]*.py` | 2122 passed, 2 skipped, **0 failed** |
| `tests/test_[s-z]*.py` | 1420 passed, 6 skipped, **0 failed** |
| `contracts/tests` | 729 passed |
| `scripts/ws_smoke.py` (after the primitives swap) | `all_passed=True` |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |
| live drive, honored release point | run `01M0SJFVRS1W71S4XDZDH29JW2`, exit 0 |
| live drive, refused release point | typed `TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN`, exit 0 |
| do_sag parity, pinned at 2.0 m3/s | run `01M0SK16B14WRRT9JYEM7JAZ1K`, **8.5772 mg/L at 10631.7 m**, violates=false, 60 points, first 9.022 / last 8.9623 - bit-identical to the ADR 0303 reference, so nothing in 3b moved the physics |
