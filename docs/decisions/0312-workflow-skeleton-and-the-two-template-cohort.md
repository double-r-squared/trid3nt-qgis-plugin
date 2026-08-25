# 0312 - The workflow skeleton (template method), hardened on a two-template cohort

Status: LANDED (cohort only - the fleet does not move until NATE redlines)
Date: 2026-08-24
Supersedes: nothing. Extends ADR 0303 (declarative library v1 + do_sag) and
ADR 0305 (river_dye migration).

## Context

Six templates had been migrated onto the declarative library, and the same ~70
lines reappeared at the bottom of every one of them: `_normalize`, `_with_notes`,
`_physical_answer`, and a try/except tool body. do_sag additionally carried the
named disease exhibit - `ReachSolve.telemac_waqtel_o2`, one plan step funnelling
seventeen explicitly-named kwargs through three files to run, imperatively, the
same reach pipeline river_dye already DECLARED.

`docs/design/declarative-workflows.md` section "The Workflow Skeleton (Template
Method)" is the contract; `docs/IDEAS.md` 2026-08-24/25 carries the rulings
(demolition clause, no-double-middleware, naming, mesh, hardening methodology).
Methodology, per NATE: inside-out, SMALL COHORT FIRST - build the skeleton and
migrate only `telemac_do_sag` + `telemac_river_dye`, iterate against NATE's
taste, and only then move the fleet.

## Decision

**`Workflow` is the skeleton.** `workflows/lib/workflow.py` holds the abstract
class that owns everything invariant: the normalize -> resolve -> interpret
spine, the post stage (provenance merge, notes), the publish stage (chart
persist, the answer artifact), the typed error envelope, and the registration
factory. It COMPOSES the existing library - gates, ledger, binding, the leak
guard, solve supervision all stay in `interpreter.py`. The no-double-middleware
law was read as applying to our own library too, so the skeleton re-implements
none of it.

**`EngineOps` is the facade: five operations and nothing else.**
`acquire_domain`, `build_mesh`, `author`, `solver_spec`, `read_results`.
`TelemacWorkflow(Workflow)` realizes them by binding the existing TELEMAC step
family. Named by engine only, per the naming ruling.

**Slots are value objects.** `MeshPolicy` (fixed-field, engine-neutral),
`Physics(process, **values)` and `Forcing(**values)` (open bundles). They are
plan-CONSTRUCTION values: the facade explodes them into the runner's real
kwargs while the plan value is built, so what reaches `Step.kwargs` is a plain
mapping the interpreter already binds - and an unknown slot member is refused
against `inspect.signature(write_reach_deck)` at plan-construction time rather
than silently vanishing from the deck.

**A chart is a function object, colocated.** `ChartSpec.builder` takes the
callable; a dotted string is REFUSED with the fix in the message (no fallback).
`build_sag_chart` moved into `do_sag.py` and `build_dye_chart` into
`river_dye.py`, each beside the plan it charts.

**The registration factory generates the tool.** `register_workflow(facade,
metadata, PARAMS, plan, data=..., answer=..., provenance=..., coerce=...,
doc=..., extra_args=...)` synthesizes the tool signature from the declared
params (`Param.type` declares the wire type where inference would be wrong;
`Param.wire=False` keeps a coercion-resolved param off the model's schema),
generates the body, and registers it. Template end state: PARAMS + DATA + plan
+ ANSWER + the chart.

**The `Workflow(name, engine=...)[...]` plan constructor DIES** (demolition
clause). The name belongs to the skeleton, and the constructor restated what
registration already knows; `plan(p, d, ops)` returns the step sequence and the
skeleton names and engines the `Plan`.

## Consequences

- do_sag's composite is gone: its pipeline is now the same declared nodes
  river_dye declares, plus one WAQTEL process step and one self-gating
  resolved-input review. `do_sag/steps.py` is deleted outright.
- The saturation clamp moved from inside the composite to
  `steps/water_quality.waqtel_o2_process`, a named step the deck and the
  postprocess both `Ref` - one resolution, two readers, which is what the old
  composite achieved by passing one local variable to both.
- Steps carry a `stage` (`acquire|prep|mesh|gates|author|solve|post|publish`), so
  `plan.describe()` reads as the universal sequence. The stamp is applied by the
  STEP-FAMILY CONSTRUCTORS - `Geocode.reach`, `WriteDeck.telemac`,
  `Solve.telemac`, `Products.*` each name their own stage - not by the facade
  operations, which assemble already-stamped steps. (The first draft of this ADR
  said the five operations stamp it; they do not, and the distinction matters for
  the mesh wave, where the enforcement will read the stamp.) The order is NOT
  enforced this iteration: the mesh gate (which sits mid-plan) does not exist yet,
  and both cohort templates gate at the front. Enforcement waits for the mesh
  wave.
- `build_mesh` returns a `MeshHandle`, not a mesh artifact: TELEMAC's corridor
  mesher still runs inside the deck writer and the worker. The interface is the
  frozen part; the shared front (`workflows/mesh`) and the BYO/mesh-gate work are
  later iterations, deliberately not built here. (Landed as `ReachMesh`; renamed
  in wave 2b - "Reach" is the banned domain qualifier one layer down from the
  facade, and the class is a handle, so it is now named for what it is. The
  corridor-shaped fields moved with it: see the placement note below.)
- An adapter bug surfaced and was fixed: `_normalize_callable_for_gemini`
  simplified `__annotations__` but `functools.wraps` copied a SYNTHESIZED
  `__signature__` over it, so `from_callable` read the simplified hints and the
  unsimplified parameters. It now re-stamps the signature. SCOPE, measured rather
  than assumed: it fixed the 2 cohort tools; the 101 spec-promoted fetchers were
  empirically UNCHANGED by it (their synthesized signatures and their simplified
  hints already agreed, so there was nothing for the re-stamp to correct). The
  first draft's "affected every synthesized-signature tool" overclaimed.
- PLACEMENT (wave 2b): `MeshPolicy` keeps only the engine-neutral SIZING ask
  (`resolution`, `target_edge_m`). `extent_km` / `width_m` / `boundary_source`
  describe a CORRIDOR, which is one domain shape among many, so they moved into a
  facade-owned `CorridorPolicy` in `workflows/telemac/` and reach `build_mesh` as
  an engine slot (`build_mesh(domain, policy, **slots)`). Likewise
  `shared/aoi.location_or_bbox` no longer defaults `code_prefix` to `"TELEMAC"`:
  the file is engine-agnostic by placement, and a default engine prefix there
  would hand a future SWMM caller TELEMAC's error codes silently.
- ENFORCEMENT (wave 2b): five promises the wave-2 skeleton made and did not keep,
  now kept. (1) `register_workflow` REFUSES a facade with an unrealized operation
  (`FacadeIncompleteError` at import), so a `NotImplementedError` never reaches a
  caller as `<ENGINE>_INTERNAL_ERROR`. (2) The slot-vs-signature check runs BOTH
  ways: a required deck field no slot covers is refused at plan construction, not
  discovered by `write_reach_deck` three fetches later. (3) `_normalize` triages
  like `run()` does - retryable propagates with its suggestions channel, typed
  keeps its code, and a bug in our own coercion reads as `INTERNAL_ERROR` instead
  of blaming the caller with `PARAMS_INVALID`. (4) A `Param` with a numeric default
  and neither bounds nor `type` is refused rather than advertised to the model as a
  STRING. (5) `_run_id` reads the step the facade DECLARES (`solve_step`) instead
  of the literal `"solve"`. Plus: `provenance=` rows are arity-checked at
  declaration, and `DataRefs`' miss is an `AttributeError` subclass so `hasattr` /
  `deepcopy` / pickle probes do not crash.
- HONESTY (wave 2b): an explicit `mesh_resolution_m` that the >= 2-cells-across
  rule caps DOWNWARD now stamps a provenance note in the bounds-clamp's own
  narration style ("mesh_resolution_m 100 CAPPED to 30 m by the channel-width rule
  (width 60 m / 2)"). Only the raise was narrated before, so the canary's own
  declaration - 100 m asked on a 60 m channel - silently modelled 30 m with the
  lever reading as honoured. The mesh is unchanged; the label is new.
- COERCION ORDER (recorded, low severity): `do_sag`'s coercion tuple now runs
  `location_or_bbox` FIRST, where the pre-migration body ran the outfall-point
  coercion first. No behavioural difference has been found - the two read
  disjoint wire keys - but the order is now the declaration's, so it is stated
  rather than incidental.
- The contract's sensor/context-LAYER hook is deliberately NOT built. It was,
  briefly, and the ADR 0244 input-surfacing sweep caught it at once: the steps
  that fetch inputs already emit through the one seam, so a skeleton-level
  second emitter is exactly the double-emission that guard exists to catch. It
  belongs to the emission-unification wave, where the seam is the single home.
  A test now pins that the skeleton emits no input layer of its own.
- Four declarative templates (`modflow_regional_water_budget`,
  `swmm_rdii_rtk_unit_hydrograph`, `swmm_snowmelt_degree_day`,
  `swmm_aquifer_baseflow_to_node`) were NOT migrated; they were adapted
  mechanically to `Plan(name, engine, (...))` and to function-object chart
  builders so the fleet stayed operable. Their own migration waves still owe
  the skeleton.

## Physics parity (R3)

Both cohort canaries were captured on the pre-migration code, then re-run on the
migrated code.

| | before | after |
|---|---|---|
| do_sag coarse - DO minimum | 9.0099 mg/L | 9.0099 mg/L |
| do_sag coarse - sag location | 158.8 m | 158.8 m |
| do_sag coarse - violates standard | false | false |
| do_sag `t2d_river.cas` | - | BYTE-IDENTICAL |
| do_sag worker manifest | - | BYTE-IDENTICAL (bar the run tag) |
| do_sag `chart_spec.json` | - | identical bar the minted `chart_id` |
| do_sag `metrics.json` | - | identical bar the run-id-derived `layer_uri` |
| river_dye coarse - peak concentration | 4.878571510314941 mg/L | 4.878571510314941 mg/L |
| river_dye coarse - peak time | 200.0 s | 200.0 s |
| river_dye coarse - plume reach | 472.7 m | 472.7 m |
| river_dye coarse - active frames / mesh | 3 / 30.0 m, 155 node estimate | 3 / 30.0 m, 155 node estimate |
| river_dye `t2d_river.cas` | - | BYTE-IDENTICAL |
| river_dye `metrics.json` / `chart_spec.json` | - | identical bar the run-id-derived `layer_uri` and the minted `chart_id` |

"155 nodes" in that table is the pre-solve ESTIMATE the autoscaler reports
(`_estimate_mesh_nodes`, calibrated to ~15%), not a count of the mesh that ran:
the coarse river_dye run's actual mesh was **1,048 nodes**. It is a parity row
because the same declaration must produce the same estimate; it is not a mesh
measurement, and the earlier wording read as one.

`scripts/drive_river_dye_cards.py` gained a `--coarse` canary declaration (the
same shape do_sag already had): short reach, short window, pinned discharge, a
derived release point, `input_mode=auto` - so a library or shared-step change is
provable end-to-end through the product path in minutes.

One representation delta was found and REMOVED rather than documented: the
generic answer rule first wrote the provenance note as `discharge_m3s_note`,
where the artifact has always called it `discharge_note`. `provenance=` now
accepts a `(param, note_key)` pair so the answer artifact keeps its own name.

## Acceptance: the workflow-only change list

The design doc's falsifiable criterion: **the ADR must ENUMERATE every
meaning-level edit class achievable in the template file with zero other
touches.** A meaning-level change that turns out to need `steps/` is a defect by
definition. The complete list, each with the mechanism that makes it
template-only:

| # | edit class | example | what makes it template-only |
|---|---|---|---|
| 1 | VALUES / defaults | `k1_per_day` default `0.3` -> `0.25` | `Param.default` is read by the resolver at the SCENARIO/CONSTANT door; no runner names a default |
| 2 | BOUNDS | `reach_length_km` `(0.5, 15.0)` -> `(0.5, 8.0)` | `resolver._finish` clamps against `Param.bounds` and writes the CLAMPED note itself; the deck writer sees only the clamped number |
| 3 | DOORS | move `channel_width_m` from CONSTANT to USER, or to DERIVED with a `resolve=` path | the door IS the field; `resolve` is a dotted path, so a new derivation is a new step-tier function the declaration NAMES, and the walk order is the library's |
| 4 | COMPOSITION / plan shape | add, drop or reorder a node in `plan(p, d, ops)`; branch it with `When` | `plan` returns a VALUE; the skeleton names and engines the `Plan` and the interpreter walks whatever it is handed |
| 5 | SLOT CONTENTS | add `tracer_diffusivity` to the `Physics` bundle | slots are open bundles unpacked at plan construction against the deck writer's real signature - a member the writer accepts needs no other edit, and one it does not is refused on the spot |
| 6 | GATES | add/remove a `FormGate` or `DrawGate`, or change which param a draw fills | gates are plan NODES on the pending-confirmation spine; the card mechanics are the interpreter's |
| 7 | CHARTS | rewrite `build_sag_chart`, add a second `.chart(...)` to a step | the builder is a FUNCTION OBJECT colocated in the file; display / persist / emit are the skeleton hook's |
| 8 | ANSWER fields | add `sag_curve_bod_mgl` to `ANSWER`, or a `provenance=` row | `Workflow.answer` reads `getattr(result, field)` over the declared tuple; the persisted metrics follow |
| 9 | DOCSTRING / ROUTING | rewrite `summary` / `routing` / `not_for` | `doc=` is rendered by `render_docstring`; the routing view and the model-facing schema are generated from the same declaration |
| 10 | WIRE SURFACE | add an alias in `extra_args`, or hide a param with `wire=False` | the signature is synthesized by `_wire_signature` from the declaration, so the schema moves with the file |
| 11 | COERCIONS | add/reorder an entry in `coerce=` | coercions are declared callables run by `_normalize` in declaration order |
| 12 | METADATA | resolution specs, gate spec, ttl/cache class | `AtomicToolMetadata` is constructed in the file and passed straight to `register_tool` |

Demonstrated live on the migrated `do_sag.py`, two of these edits, each with
`git diff --name-only` showing ONE file:

1. class 2, a BOUND - `reach_length_km` bounds `(0.5, 15.0)` -> `(0.5, 8.0)`: a
   12 km ask resolved to 8.0 with the note "CLAMPED from 12 to the declared
   maximum 8 km";
2. class 7, a CHART - the sag caption's grade wording: the built payload carried
   the new sentence.

Both reverted.

NOT on this list, and deliberately: anything that changes what the ENGINE can
express (a new deck keyword, a new solver module, a new reader) is a mechanism
change and touches exactly one runner in `<engine>/steps/`. The list above is
about MEANING, not capability.

The boundary is easy to blur, so wave 2b's own mesh-cap row is worth stating as
the counter-example it is: DECLARING `("mesh_resolution_m", "mesh_resolution_note")`
in `provenance=` was class 8 and touched one file, but the row it lifts had to be
PRODUCED first, which is mechanism and landed in `steps/products.py`. A template
can declare what it wants on the answer; it cannot conjure a provenance row no
step emits. Class 8 is template-only for a field the run already carries - and
that is exactly the distinction the criterion is testing.

### The regression figure

THE GATE IS THE FAILURE SET, NOT A PASS COUNT. The offline suite passes when its
FAILED set matches the documented baseline EXACTLY - **six** failures, by name:
4 `test_fetch_resolution_gate` in `[f-o]` plus 2 `test_run_river_dye_scenario` in
`[p-r]`, with `[a-e]` and `[s-z]` clean. A seventh failure is a regression even if
the pass count went up; a missing one is a silently-skipped test, not a fix. Set
equality is the whole check.

PASS COUNTS ARE INFORMATIONAL AND THEY WOBBLE. Collection is env-conditional -
skips vary with what is installed, with `.env.local` presence, and with which
optional engine libraries the venv holds - so the same tree reports different
totals on different runs. This document first cited **11963** at the wave-2
landing; an earlier draft of it cited 11990; the wave-3 review panel reports
observing **11987**, **12026** and **12051** on this tree, all with the failure
set intact. None of those five numbers is a gate and none contradicts the others.
What is measurable and stable is the COLLECTION ceiling every pass count sits
under:

    ./venvs/agent/bin/python -m pytest tests -q --collect-only | tail -1
    # 12118 tests collected in 11.13s   (at b24feb64, .env.local present)

An earlier version of this paragraph read "**11963 passed**, against the standing
baseline of exactly 4 environmental `fetch_resolution` failures", which got the
gate wrong twice: it led with the count, and its "4" was the `[f-o]` slice's
share rather than the suite's six. Corrected 2026-08-25; the reading it was
reaching for - that the count is evidence only alongside the set - was right and
is now the rule rather than the caveat.
