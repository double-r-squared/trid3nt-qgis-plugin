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
- Steps carry a `stage` (`acquire|prep|mesh|gates|author|solve|post|publish`)
  stamped by the facade's five operations, so `plan.describe()` reads as the
  universal sequence. The order is NOT enforced this iteration: the mesh gate
  (which sits mid-plan) does not exist yet, and both cohort templates gate at
  the front. Enforcement waits for the mesh wave.
- `build_mesh` returns a `ReachMesh` HANDLE, not a mesh artifact: TELEMAC's
  corridor mesher still runs inside the deck writer and the worker. The
  interface is the frozen part; the shared front (`workflows/mesh`) and the
  BYO/mesh-gate work are later iterations, deliberately not built here.
- An adapter bug surfaced and was fixed: `_normalize_callable_for_gemini`
  simplified `__annotations__` but `functools.wraps` copied a SYNTHESIZED
  `__signature__` over it, so `from_callable` read the simplified hints and the
  unsimplified parameters. It now re-stamps the signature. This affected every
  synthesized-signature tool, spec-promoted fetchers included.
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

One representation delta was found and REMOVED rather than documented: the
generic answer rule first wrote the provenance note as `discharge_m3s_note`,
where the artifact has always called it `discharge_note`. `provenance=` now
accepts a `(param, note_key)` pair so the answer artifact keeps its own name.

## Acceptance: the workflow-only change list

Demonstrated live on the migrated `do_sag.py`, two meaning-level edits, each
with `git diff --name-only` showing ONE file:

1. a BOUND - `reach_length_km` bounds `(0.5, 15.0)` -> `(0.5, 8.0)`: a 12 km ask
   resolved to 8.0 with the note "CLAMPED from 12 to the declared maximum 8 km";
2. a CHART - the sag caption's grade wording: the built payload carried the new
   sentence.

Both reverted.
