# ADR 0303 - Declarative library v1 + the do_sag migration

Status: LANDED (wave 1 of the declarative campaign)
Design: `docs/design/declarative-workflows.md` (NATE-ruled; this ADR implements
migration-order items 1 and 2, minus the plugin form/draw cards - wave 2).

## Context

Workflow files were imperative composers: argument coercion, inline clamps,
hand-written provenance stamping, hand-wired gates and a bespoke error tail, all
in the same function that orchestrated the physics. The design doc's ruling is
that a workflow is a VALUE - `PARAMS` + `DATA` + a pure `plan(p, d)` - and that
one interpreter walks it.

## Decision

### 1. `trid3nt_server/declarative/` - the library

| module | lines | what it owns |
|---|---|---|
| `params.py` | 155 | `Param` (door, bounds, units, resolve path, user_lever, optional, law-9 consequence), `ResolvedParam`, `ResolvedParams`, the six `doors` |
| `data.py` | 109 | `Data` + producers. `ReferenceProducer` has NO `.byo()`; `AuthoredProducer` does - modifier legality is the type, not a runtime check |
| `plan.py` | 263 | `Step`, `Gate`/`FormGate`/`DrawGate`, `When`, `Ref`, `Within`, `Transparent`, `RenderSpec`, `ChartSpec`, `Workflow[...]`, and the `.named`/`.overrides_domain`/`.render`/`.chart` modifiers |
| `validate.py` | 120 | the plan validator - Ref integrity (incl. forward-ref refusal), duplicate names, gate placement, DrawGate/param door agreement, Data producer refs |
| `resolver.py` | 193 | the six doors with bounds clamping + provenance rows (`SyntheticInput`) |
| `interpret.py` | 404 | the interpreter: node expansion, ledger replay, gates, Data production, Ref binding, Domain rebinding, typed error envelopes |
| `ledger.py` | 123 | `StepLedger` on the existing `FileMCPClient` document store |
| `domain.py` | 58 | the `Domain` environment (contextvar), read implicitly |
| `docstring.py` | 65 | the registered tool's docstring, rendered from the declarations, routing block front-loaded inside the 1000-char truncation budget |
| `errors.py` | 62 | the typed error family |
| `__init__.py` | 61 | the public surface |
| **total** | **1613** | one-time library cost |

Seams REUSED, not reimplemented: `substep`/`begin_substeps`/`current_emitter`/
`emit_chart_payloads` (emission), `gate_input_review`/`resolve_input_gate_mode`
(the input-review gate spine), `SyntheticInput` (law-9 provenance),
`publish_layer` (the one raster-styling chokepoint), `FileMCPClient`
(persistence). The GateSpec idiom - name providers by DOTTED IMPORT PATH so the
declaration stays serializable and engine knowledge stays in the engine - is
used for step runners, Data producers, param derivations and chart builders.

Rulings implemented as written: doors 1-6 with BYO/USER never ambient; immutable
recipes (the agent supplies only `p`); resume-from-failed-step as the default
(`restart_clean` is the flag); reference data has no `.byo()`; the chart SPEC is
the product (no server-side figure).

Design points the doc did not spell out, decided here:

- **Door order is PRECEDENCE, not evaluation order.** A derivation may read any
  other param, so labeled defaults are seated before derivations run. A derived
  param still competes only with its own fallbacks, never another param's, so
  precedence is unchanged. Derivations resolve to a fixpoint, so `PARAMS` needs
  no dependency ordering.
- **Door 6 refuses at the plan, not at the resolver.** An unresolved required
  param becomes a `required_missing` row; the DrawGate refuses it typed (auto) or
  names wave 2 (user_gated), and any still-missing required param refuses
  immediately before the first `consequential` step. A resolver that refused
  eagerly would fire before the gate that exists to fill the value.
- **One execution NODE per step body, per declared render, per declared chart.**
  The ledger therefore replays an expensive solve while a cheap chart
  re-executes - which is what makes resume worth having on a plan whose solve is
  the only long step.
- **A non-numeric value for a bounded param REFUSES.** The imperative
  `try/except -> default` swallowed a bogus argument into a physics value; that
  is the law-9 class.

### 2. `telemac_do_sag` migrated

The tool keeps its name, its registry entry, its `AtomicToolMetadata`, its
`ResolutionSpec` and its `TemplateCard`. Its body is now: normalize the wire args
-> `resolve_params` -> `plan(p, d)` -> `validate_plan` -> `interpret`. The old
composer body is deleted.

`do_sag` delegates its whole physical pipeline to `model_telemac_river_dye`, so
in v1 the reach pipeline is ONE composite step and `DATA = ()`: its internal
fetches surface through the existing emit-on-fetch seam and become declared
`Data` when river_dye is migrated (wave 3). Declaring Data the composite secretly
fetches would have been a lie.

## Inventory - every old behavior re-homed or documented-deleted

| # | old behavior (do_sag.py @ HEAD) | door / re-homing |
|---|---|---|
| 1 | `bbox` coercion via `coerce_bbox_value`, alpha-string-as-location rescue | `_normalize` in the tool body - wire-arg normalization, before any door |
| 2 | `TELEMAC_PARAMS_INVALID` on an uncoercible bbox | `_normalize`, byte-identical envelope |
| 3 | `TELEMAC_PARAMS_INCOMPLETE` when neither location nor bbox | `_normalize`, byte-identical envelope |
| 4 | "location wins" when both supplied | `_normalize`, unchanged |
| 5 | `location` | Param, door=QUESTION, optional, consequence=aoi |
| 6 | `bbox` | Param, door=USER, optional, consequence=aoi |
| 7 | `_clamp(discharge_bod_mgl, 0.1, 5000, 20)` | Param bounds `(0.1, 5000.0)`, door=SCENARIO default 20.0 |
| 8 | `_clamp(water_temp_c, 0, 40, 20)` | Param bounds `(0.0, 40.0)`, door=SCENARIO default 20.0 |
| 9 | `_clamp(do_standard_mgl, 0, 15, 5)` | Param bounds `(0.0, 15.0)`, door=SCENARIO default 5.0 |
| 10 | `_clamp(k1_per_day, 0.01, 20, 0.3)` | Param bounds `(0.01, 20.0)`, door=SCENARIO default 0.3 |
| 11 | `_clamp(k2_per_day, 0.01, 50, 0.9)` | Param bounds `(0.01, 50.0)`, door=SCENARIO default 0.9 |
| 12 | `_clamp(reach_length_km, 0.5, 15, 12)` | Param bounds `(0.5, 15.0)`, door=SCENARIO default 12.0, consequence=aoi |
| 13 | `_clamp` returning the DEFAULT on a non-numeric arg | CHANGED, deliberately: a non-numeric value for a bounded param now REFUSES typed. Silently defaulting a physics value on a typo is the law-9 class. |
| 14 | `_do_saturation_mgl(temp)` (Elmore-Hayes) | `steps.do_saturation_mgl`, reached as Param `do_saturation_mgl` door=DERIVED, user_lever |
| 15 | `do_saturation_mgl` explicit override | door 1 short-circuits the derivation - same precedence, no branch |
| 16 | `up_do = upstream_do_mgl if set else sat` | `steps.upstream_do_mgl`, Param door=DERIVED, user_lever |
| 17 | `up_do = min(max(up_do, 0), sat)` | kept in `steps.solve_waqtel_o2` + logged: it is a coupling between two params, so it cannot be a declared static bound. A static `(0, 20)` mg/L bound is declared as well. |
| 18 | `channel_width_m=60.0` inline default | Param door=CONSTANT default 60.0, bounds `(1, 5000)`, consequence=numerical |
| 19 | `sim_duration_s=10800.0` inline default | Param door=CONSTANT default 10800.0, consequence=numerical |
| 20 | `mesh_resolution="auto"` | Param door=CONSTANT default "auto" |
| 21 | `mesh_resolution_m` | Param door=USER, optional, user_lever, bounds `(3, 5000)` matching the ResolutionSpec floor |
| 22 | `bank_source="nhd_area"` | Param door=CONSTANT default "nhd_area", consequence=scenario. The real-vs-assumed PHYSICS row stays river_dye's (it stamps `basis=fetched` only once banks actually resolve). |
| 23 | `discharge_m3s` | Param door=USER, optional, consequence=physics. Absent -> no provenance row here; river_dye stamps the NWM-resolved value. |
| 24 | `compute_class="medium"` | Param door=CONSTANT default "medium" |
| 25 | `input_mode` | NOT a Param - it is the gate-mode lever; passed through to `interpret(input_mode=...)` and to the reach pipeline |
| 26 | `do_sag_config` dict assembly (incl. `k2_formula=0`) | `steps.solve_waqtel_o2` - deck serialization is the step's job |
| 27 | the four hand-written `SyntheticInput` WQ rows with per-param `basis="default_demo" if v == <literal> else "user"` | `resolver.provenance_entries` - basis comes from the DOOR the value came through, so the literal comparisons die. Consequence tags preserved exactly: bod/temp = scenario, k1/k2 = numerical (so auto mode still proceeds labeled, never refuses). |
| 28 | `layer.model_copy(update={"synthetic_inputs": ...})` | unchanged, now fed by `RunResult.entries` |
| 29 | `except (TelemacBanksUnavailableError, TelemacReachDegenerateError): raise` | the interpreter re-raises `DeclarativeError` untouched and wraps everything else in `StepFailedError` carrying the engine's own `error_code`, so the tool returns the same typed envelope. The two re-raised types now arrive as `status=error` dicts with their own `error_code` rather than propagating - see "behavior deltas". |
| 30 | `except (TelemacDyeScenarioError, PostprocessTelemacError, RunTelemacError)` -> error dict | `StepFailedError` carries `getattr(exc, "error_code")`, so the envelope's `error_code` is preserved |
| 31 | `except Exception` -> `TELEMAC_INTERNAL_ERROR` | kept in the tool body |
| 32 | `asyncio.CancelledError` re-raised | kept, in both the tool body and the interpreter |
| 33 | the completion log line | kept, plus `executed=`/`replayed=` |
| 34 | the rich LLM-facing docstring | GENERATED by `render_docstring` from the Param descs + the routing block. Routing block measured at 936 chars - inside the 1000-char truncation budget. |
| 35 | `TEMPLATE_CARD` | kept verbatim, plus `outfall_coords` in the knobs list |
| 36 | `_TELEMAC_DO_SAG_RES_SPEC` / `_TELEMAC_DO_SAG_METADATA` | kept verbatim |
| 37 | `_maybe_emit_do_sag_chart` (in river_dye.py, do_sag-only) | RE-HOMED to `steps.build_sag_chart` and declared as `.chart("do_sag_curve", ...)`. Deleted from river_dye (`DELETION_LEDGER`). |
| 38 | `_postprocess_and_publish_do_sag(location_name=...)` | parameter deleted - the chart was its only reader |
| 39 | (new) the outfall location | Param `outfall_coords`, door=USER, optional, user_lever, with a `DrawGate(geometry="point")` declared. Wired to river_dye's existing `release_seeds_reach` / `seed_release_lon` / `seed_release_lat`. |

### Behavior deltas (deliberate, three)

1. **Non-numeric bounded arg refuses** instead of silently defaulting (row 13).
2. **`TelemacBanksUnavailableError` / `TelemacReachDegenerateError` no longer
   propagate as exceptions**; they return the standard `status=error` envelope
   carrying their own `error_code`. The message the user sees is unchanged. The
   old propagate-don't-catch was a special case for two of the five typed errors
   the same pipeline raises.
3. **The sag-chart title now uses the user's own `location` words** rather than
   the geocoder's display name (the layer, which the chart now reads, does not
   carry the geocoded display name). Door 2's principle - the user's words beat
   data - so this is the better source; recorded because it is a visible change.

### The parked fork - the outfall in auto mode

The kickoff asked that the discharge/outfall become a USER-door param whose
absence is a TYPED REFUSAL in auto mode. Implemented as USER-door + DrawGate but
**optional**, i.e. absent -> the geocoded reach seed (today's behavior), because:

- a required-in-auto outfall makes every existing invocation
  (`telemac_do_sag(location=...)`) refuse, which fails the doc's own acceptance
  (b) - "same question -> same physical answer within tolerance" - a typed
  refusal is not a physical answer;
- the reach top is not INVENTED, it is DERIVED from the geocode, which is door 3.
  Door 6 (refuse) is only reached when doors 1-5 are all empty.

NATE ruling wanted: make `outfall_coords` REQUIRED in auto mode (a breaking
change to the tool's contract and to the showcase invocations), or keep the
derived reach-seed fallback as landed. The library supports both - flipping
`optional=False` on one Param declaration is the whole change.

## Consequences

### Net LOC

| | before | after | delta |
|---|---|---|---|
| library (one-time) | 0 | 1613 | +1613 |
| `do_sag/do_sag.py` | 314 | 295 | -19 |
| `do_sag/steps.py` | 0 | 165 | +165 |
| `river_dye.py` (chart re-home) | - | - | -54 |
| **per-workflow subtotal** | **314** | **460** | **+92** |
| tests (`test_declarative_library.py`) | 0 | 437 | +437 |

Honest reading: do_sag is the WORST case for the net-LOC law. It is a 314-line
parameter front-end with no orchestration of its own, so there is no imperative
plumbing to delete - the migration trades imperative coercion for declarations
roughly one-for-one, and adds the outfall capability and the re-homed chart. The
library's 1613 lines are a one-time cost amortized across the family; the
TELEMAC baseline is 12,139 lines / 8 templates and river_dye alone is 3,503,
which is where the deletion lives. The engine family's net-negative verdict is
due at the end of the TELEMAC campaign, not here.

### Coded tools

No change: `telemac_do_sag` keeps its single registry entry. No tool added, none
removed.

## Acceptance evidence

Question, both runs: "Eel River near Scotia, California", BOD 20 mg/L, 20 C,
standard 5 mg/L, k1 0.3, k2 0.9, 12 km reach, mesh auto. Real NHDPlus reach,
real NHDArea banks, carrier discharge 2 m3/s from the NOAA National Water Model,
mesh h=14 m / ~8543 nodes, local-docker `trid3nt-local/telemac:latest`.

Note: the committed showcase reach for this template
(`Sacramento River near Colusa, California`) now typed-refuses with
`TelemacBanksUnavailableError` - no NHDArea polygon covers it. That is the
honest refusal working, not a regression, but the showcase seed is stale.

**(a) Inventory** - the 39-row table above; every behavior re-homed, with three
deliberate deltas recorded.

**(b) Old vs new** - the same physical answer, bit-identical, not merely within
tolerance:

| | OLD (`52b3b787`) | NEW (declarative) |
|---|---|---|
| run id | `01M0R85WYRJEQ2XST7NHK8W6EA` | `01M0RA0RCXW4S40PN1RBSSPJ6M` |
| DO sag minimum | 8.5772 mg/L | 8.5772 mg/L |
| sag location | 10631.7 m downstream | 10631.7 m downstream |
| violates the 5 mg/L standard | false | false |
| sag-curve points | 60 | 60 |
| curve first / last DO | 9.022 / 8.9623 mg/L | 9.022 / 8.9623 mg/L |

**(c) Live end-to-end** - the new library ran the full chain and returned a
published `TelemacDoLayerURI`
(`s3://trid3nt-runs/01M0RA0RCXW4S40PN1RBSSPJ6M/telemac_do_field.tif`,
`style_preset=continuous_dissolved_oxygen`, units mg/L, bbox
`(-124.16498, 40.49583, -124.08739, 40.58685)`). The declared chart node built
the SPEC - a two-layer vega-lite value (DO + CBOD lines, 120 points, plus the
5 mg/L standard as a dashed rule), title "Dissolved-oxygen sag - Eel River near
Scotia, California". No server-side figure.

**(d) Resume** - `scripts/prove_declarative_resume.py`, one solve, two passes:

```
PASS 1 (chart builder forced to raise) -> STEP_FAILED
LEDGER after pass 1: [(2, 'do_field',
    ('s3://trid3nt-runs/01M0RA0RCXW4S40PN1RBSSPJ6M/telemac_do_field.tif',))]
PASS 2 (same invocation, unpatched):
    executed=['do_field.chart:do_sag_curve'] replayed=['do_field']
```

The ~27-minute TELEMAC solve replayed from the ledger's cached artifact in
milliseconds; execution resumed at the failed chart node. The persisted ledger
carries the layer as `result_kind=pydantic`
(`trid3nt_contracts.telemac_contracts.TelemacDoLayerURI`) with its artifact URI,
and the chart node as `{"chart": "do_sag_curve", "emitted": true}`.

**(e) Net LOC** - the table above.

### Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1536 passed, 5 skipped, 0 failed (baseline 0) |
| `tests/test_[f-o]*.py` | 6645 passed, 3 skipped, 1 xfailed, **4 failed** - all `test_fetch_resolution_gate.py` (baseline) |
| `tests/test_[p-r]*.py` | 2102 passed, 2 skipped, **2 failed** - both `test_run_river_dye_scenario.py` (baseline) |
| `tests/test_[s-z]*.py` | 1407 passed, 6 skipped, 0 failed (baseline 0) |
| `contracts/tests` | 721 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True`, case self-cleaned |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |

Exactly the 4 + 2 baseline failures, no others. No `workers/` path touched, so no
image rebuild is in play.

## Not in v1

Plugin form/draw cards (wave 2 - the DrawGate raises a typed
`GATE_NOT_YET_SUPPORTED` naming wave 2 when a REQUIRED drawn param is missing in
`user_gated` mode, rather than silently skipping). Full pause/walk-away/resume.
`Data` declarations for the reach pipeline (wave 3, with river_dye).
