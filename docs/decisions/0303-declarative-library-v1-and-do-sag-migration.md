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

---

# Correction - wave 1b (adversarial review)

Status: LANDED. An adversarial verifier REFUTED the wave-1 landing on five
blockers and fourteen observations, all probe-proven. This section records what
the corrections CHANGED about the decisions above; the sections before it are
left as written, so the delta is readable.

## The ledger is not a cache (blocker 1)

Wave 1 wrote a ledger record per completed node and never marked the plan
finished, so a SUCCESSFUL invocation replayed forever. The persisted proof: one
document under `data/persistence/trid3nt_dev/declarative_run_ledgers.json` with
BOTH of `do_field`'s nodes recorded and no completion state - which made
`telemac_do_sag`, a `cacheable=False` / `live-no-cache` tool, hand back the same
COG and the same NWM-derived discharge across sessions, and would hand back a
dead `s3://` URI once the run objects were pruned.

The ruling in force is resume-from-the-FAILED-step. That is now what the ledger
holds, and only that:

- **A plan that reaches its end reaps its own ledger** (`StepLedger.complete()`
  deletes the invocation's document). The alternative - keep the records and
  stamp `complete: true` - was rejected: a marker that is only ever read as "do
  not use this" is a tombstone, and tombstones accumulate one document per
  distinct question forever. Deleting IS the completion marker.
- **Replay probes the artifact.** Before a cached record is adopted, every
  `artifact_uris` entry is probed (`head_object` for `s3://`, `os.path.exists`
  for a bare path, off-loop via `asyncio.to_thread`). A missing artifact logs a
  warning and re-executes the node. A replay that returns a URI whose object is
  gone is a dead handle wearing a success envelope.
- **Eviction** (observation 14): the ledger collection is swept on every load -
  documents of a superseded schema or older than the 7-day resume TTL are
  deleted. Schema is at 2, so wave-1 documents are swept on first contact. The
  file-persistence shim gained `delete-one` for this; it had insert/update/find
  only.
- **`restart_clean` is KEPT, not redundant.** Completion-clearing removes the
  successful-run case; the flag still names the remaining one - a PREVIOUS
  FAILED attempt whose cached artifact exists but is not wanted. It is now
  documented in the generated docstring, under a new `Run controls:` block that
  also carries `input_mode` (observation 13).

Consequence worth stating plainly: **do_sag can never resume.** Its plan has one
recordable node (the terminal solve) and one auxiliary node, and an auxiliary
failure is no longer fatal (delta 4), so there is no failure that leaves a
resumable ledger. `scripts/prove_declarative_resume.py`, which proved wave 1's
resume by forcing the chart to raise, is DELETED - its premise is contradicted by
delta 4. Resume/replay/probe/domain-restore are pinned offline in
`tests/test_declarative_library.py`; the live proof rides wave 3, where
river_dye's multi-step plan gives a failure with something behind it.

## Data producers run after the gates, and are ledgered (observation 10)

Wave 1 produced the independent `Data` set before the first node - i.e. before
the plan's own gates - so a producer fetched against the very params the form
gate exists to change, and refetched on every resume.

v1 semantics, now: the eager batch fires at **the first node after the LAST
gate**; anything an earlier step needs is still produced lazily on its first
`Ref`; and every produced artifact is ledgered under its own `data:<name>` key,
so a resumed attempt does not refetch it. do_sag declares `DATA = ()`, so this
is inert here and load-bearing for wave 3.

## Behavior deltas - REVISED

Delta 1 (non-numeric bounded arg refuses) stands, and is now airtight:
`bool` bypassed it (`float(True) == 1.0`), so a flag passed to a bounded param
silently became a physics value. Bools are refused (observation 1).

**Delta 2 is REVERTED.** Wave 1 converted `TelemacBanksUnavailableError` /
`TelemacReachDegenerateError` into flat `status=error` envelopes, calling the old
propagate-don't-catch "a special case for two of the five typed errors". It was
not a special case - it was the channel. Both declare `retryable = True` and
carry `.suggestions`, and `summarize_tool_result` harvests both off the RAISED
exception. Flattening them stripped the retry flag and the recovery options
(`bank_source="constant_ribbon"`, a longer reach, an explicit river name) and
left the model an unactionable error string.

The rule now, stated generally rather than by type name: **an exception
declaring `retryable` propagates**; everything else becomes `StepFailedError`
carrying the engine's own `error_code`. The interpreter re-raises it, and the
tool body re-raises it ahead of its catch-all. The envelope conversion stays for
genuinely terminal errors.

Delta 3 (the chart title source) stands, with the bbox-only case fixed: falling
back to the layer's `name` produced "Dissolved-oxygen sag - Dissolved oxygen sag
(reach)". With no location words the layer's own name IS the title
(observation 12).

**Delta 4 - NEW: an auxiliary node failure is not fatal.** Wave 1 let a chart or
render node kill the whole run: a 27-minute solve returned an error envelope
because a chart builder threw. That contradicts the emission doctrine's failure
retracts nothing. Chart and render nodes are AUXILIARY: a failure logs a loud
warning, appends a note to `RunResult.notes`, and execution continues. The
primary result stands, and the tool merges the notes into the layer's
`fallback_note` so the miss is narrated rather than hidden. A failed auxiliary
node is not ledgered, so a rerun retries it. Two silent holes close with it: a
render whose source is not an object-store raster now raises
`RENDER_SOURCE_UNRENDERABLE` instead of returning `{"published": False}` to
nobody (observation 8), and a chart builder that produces nothing raises
`CHART_NOT_BUILT` (the honest "there was no curve to draw", said out loud).

## The rest of the observations

| # | what was wrong | what it is now |
|---|---|---|
| 2 | garbage `outfall_coords` fell back to the derived reach seed - the swallow class this wave outlaws | `coerce_outfall_point` refuses malformed input typed (`TELEMAC_PARAMS_INVALID`); ABSENT still derives |
| 3 | the derived outfall left NO provenance row (`provenance_entries` skipped value-None) | `Param.derived_when_absent` names the stand-in; an absent optional param emits a `basis=derived` row. do_sag's says the seed is mid-reach on the fetched flowline, else the geocoded centroid, and that the sag distance is measured from there |
| 4 | the derivation fixpoint's bare `except AttributeError` masked real bugs inside derivations as dependency waits | `ResolvedParams.__getattr__` raises `ParamNotResolved`; only that continues the fixpoint, every other AttributeError propagates |
| 5 | `LedgerRecord.domain` was written and never read | a replayed `.overrides_domain()` step restores the RECORDED domain (falling back to re-reading the result). Recording moved after adoption, so the record holds the domain the step LEFT, not the one it started under - the field's docstring was describing behavior it did not have |
| 6 | a `Ref` into an untaken `When` branch validated and then failed at run time; duplicate `Param` names silently last-won | Ref checking is branch-scoped (a name defined inside a `When` body is visible only inside it); duplicate param declarations are refused by both `validate_plan` and `resolve_params` |
| 7 | `ChartSpec.x` / `.y` were declared, stored, and never read | DELETED. The builder writes the vega-lite encodings; a spec field nothing reads is a lie about who owns the axes |
| 9 | a USER-door param seated from its own declared default was stamped `basis=user` | a value seated from a DECLARED DEFAULT is stamped `default_demo` whatever door it hangs under. The door says who may override it, not where the value came from |
| 11 | `bank_source` produced two contradictory provenance rows (do_sag's declared constant + river_dye's fetched row) | `merge_provenance` at the seam: the composite's own row wins on a name collision |

## Blocker 2 - `input_mode` reached nothing

`ReachSolve.telemac_waqtel_o2` omitted `input_mode`, so `model_telemac_river_dye`
received `None` and the user_gated review of the NWM carrier discharge and the
bank source - the physically dominant reviewable inputs, gated deliberately
before a 27-minute solve - was silently lost.

It is not a Param (it governs whether the run pauses; it is not a physical
value), so the fix is a declared read of the run environment: **`RunMode`**, a
plan-value sentinel the interpreter binds to the run's resolved gate mode. The
plan says `input_mode=RunMode` and the lever arrives. Same shape as `Transparent`
and `CoversAOI`: a sentinel that means something to the interpreter and stays
inspectable in the plan value.

## Blocker 5 - nested `When(False, ...)` executed

`Plan.flat()` guarded only the TOP-level condition and then recursed into nested
`When` bodies unconditionally, so `Workflow[When(True, When(False, inner))]`
executed `inner`. One flatten now takes a `taken_only` flag: `flat()` drops
untaken branches at every depth, `declared()` keeps them all. Foundation-critical
for river_dye, whose plan is conditional throughout.

---

# Correction - wave 1c (second adversarial review)

Status: LANDED. A second adversarial verifier REFUTED the wave-1b landing on
three blockers and eight observations, all probe-proven. As before, the sections
above are left as written and this one records the delta.

## Blocker 1 - a form-gate revision could not reach the run

`ResolvedParams.__getattr__` returned the concrete VALUE, so `plan(p, d)` baked
floats into `Step.kwargs` at construction - which happens BEFORE the gates the
plan itself declares. The approved edits on `gate_input_review`'s outcome were
then dropped on the floor: the step ran on the pre-review sheet while the result
was stamped with the reviewed one. What-was-approved == what-ran was false.

**LATE BINDING.** `p.<name>` now yields a `ParamRef` - a plan value in the same
family as `Ref`, `RunMode` and `CoversAOI`. The declarative purity actually
demanded it: a plan DESCRIBES, the interpreter SUBSTITUTES. `plan()` stays pure
(a `ParamRef` is a value, constructed and inspected, never executed), and the
interpreter resolves each one at node execution from the CURRENT param state.

- A `FormGate` outcome's revisions are re-seated through the resolver
  (`reseat_revised`), so DECLARED BOUNDS AND THE NON-NUMERIC REFUSAL STILL
  APPLY - the form is an edit surface, not a bypass - and every genuinely
  changed row is re-stamped `basis=user` with a `revised at input review` note.
  The run's provenance rows are then rebuilt from the approved sheet.
- An approved revision is a DIFFERENT invocation, so the ledger is re-keyed on
  the revised values: a replay can only ever come from an attempt at the values
  that were approved.
- Derivations and chart builders receive a `ParamValues` view instead, whose
  `v.<name>` IS the value. Two types rather than one flag, so a
  plan-construction read cannot silently collapse into an early-bound value.
- `bool(ParamRef)` REFUSES, naming `p.get(name)`, and `When(ParamRef, ...)`
  refuses at construction. A construction-time branch reads the value
  explicitly; it never silently branches on a description being truthy.
- The validator checks every `ParamRef` names a declared param.

### The double-gating half

`do_sag`'s plan declared a `FormGate` in FRONT of a composite
(`model_telemac_river_dye`) that runs its own input review. The composite's gate
is the one that works - and the one that matters, since the reviewable inputs
(the NWM carrier discharge, the resolved bank source) do not exist until the
composite has fetched them. The plan-level card was a second review whose edits
died.

`do_sag`'s plan `FormGate` is REMOVED; `input_mode=RunMode` threads the lever to
the composite's own gate (the wave-1b B2 fix). `Step.self_gating` declares the
property, and `_check_gate_declarations` REFUSES a plan that puts a `FormGate` in
front of a self-gating step. `FormGate` stays in the library for wave-3
workflows whose steps are library-native - and is now revision-capable.

One thing the removed gate was also doing had to be re-homed: `gate_input_review`
runs the law-9 physics refusal over the entries it is handed, so a plan with no
form card would have lost that check on its OWN declared rows. The interpreter
now runs it directly before the first consequential step whenever the plan
declares no `FormGate` (auto mode - which is the mode the refusal's own text
addresses; user_gated pauses at the composite's gate with the user present).
Vacuous for do_sag today, since its only physics-consequence param is
user-supplied-or-absent, but the floor no longer depends on a gate being
declared.

## Blocker 2 - reap-as-completion could not express FINISHED

Wave 1b made completion a DELETE. Three proven paths left a finished run's
ledger replayable anyway: a swallowed delete failure (it only warned), a crash
between the last record and `complete()`, and a `CancelledError` skipping
`complete()`. Each resurrects the B1 replay ghost - a `cacheable=False` tool
handing back a stale COG.

**A COMPLETION TOMBSTONE.** `complete: true` is written as a marker document
through the same atomic path as the records, and replay now requires the
document to be PRESENT AND NOT COMPLETE. Two details make it airtight:

- The tombstone is stamped IN THE SAME WRITE as the LAST recordable node's
  record, which is what closes the crash/cancel window - there is no interval in
  which a finished plan's records exist un-marked.
- If the completion write itself fails, that is logged at ERROR (not warning)
  and falls back to deleting the document, which says the same thing.

This answers wave 1b's own anti-tombstone objection ("a marker only ever read as
do-not-use is a tombstone, and tombstones accumulate forever"): the TTL sweep,
which wave 1b built, reaps tombstones on AGE. Accumulation is bounded by the
7-day window, not unbounded.

Two corrections to the wave-1b text:

- **"do_sag can never resume" is FALSE.** Its chart node makes a WS round trip
  (`emit_chart_payloads`); a cancel there is a `BaseException`, so it is not
  swallowed by the auxiliary-failure path and it leaves the recorded solve
  behind. That is a real resume window on a 27-minute solve.
- **A CANCELLED run correctly stays resumable.** Cancel mid-plan is not
  completion; keeping the completed steps IS resume working. Only the window
  AFTER the final record needed closing, and the tombstone closes it.

## Blocker 3 - the ledger was not concurrency-safe

`FileMCPClient` allocated its locks PER INSTANCE, and `update-one` rewrote the
whole store. Two ledgers over one collection therefore did not serialize, and a
write computed from a stale snapshot resurrected documents another ledger had
just reaped - a finished run replayable again.

Scoped to `FileMCPClient`, no new backend:

- The lock registry is module-level, keyed by resolved collection path, so every
  client shares one lock per store. It is keyed by RUNNING LOOP as well (weakly,
  so a finished loop's locks go with it), because an `asyncio.Lock` can only be
  waited on from the loop that first suspended on it.
- Every operation is now one `_cycle`: an advisory `fcntl` lock on a sidecar
  file (the store's own inode is replaced by `_atomic_write`, so a lock on it
  would be dropped), then READ, then mutate, then write - all inside the lock.
  The read-merge-write rule is structural: no operation can write a store it
  read before the lock was held.

## The observations

| # | what was wrong | what it is now |
|---|---|---|
| 1 | an eager `Data` producer ran outside any node's body, so its exception escaped the typed family entirely (no `StepFailedError`, no preserved `error_code`) | producers go through the same `_call_runner` seam as steps: retryable errors propagate, everything else becomes `StepFailedError` with the producer's own `error_code` and `step="data:<name>"` |
| 2 | accumulated auxiliary notes died when a later consequential step raised - the failure narration never mentioned the products also missing | the notes are attached to the raised exception (`add_note`), and the tool body renders them into the error envelope's message |
| 3 | `RENDER_SOURCE_UNRENDERABLE` conflated "the styling failed" (auxiliary, fine) with "the step produced no raster at all" (a primary defect) | SPLIT. `RenderSourceMissingError` / `RENDER_SOURCE_MISSING` is FATAL even though the render node is auxiliary - a declared render with no raster behind it means the step did not make the map layer it promised (honesty floor). A `publish_layer` failure over a real raster stays an auxiliary note (`RENDER_STYLE_FAILED`) |
| 4 | `Gate.constrain` / `Within` were declared, validated, and never read | REMOVED from the v1 surface. Enforcement is not possible at gate time - the geometry to constrain against is produced AFTER the gates (wave 1b, observation 10) - and the wave-2 draw card is what will both collect and constrain the drawn value. No dead declarations in the foundation |
| 5 | `RenderSpec.zero` / `Transparent`: same class - a declared no-op | REMOVED. `publish_layer`, the one styling chokepoint, has no zero-handling knob to declare against; zero-as-transparent returns with the render toolset that adds one |
| 6 | `_artifact_exists` answered one bool for "gone" and "the store is down", so a MinIO blip silently discarded a 27-minute resume with the same log line as a pruned object | `_artifact_state` answers `live` / `absent` / `unreachable`. Both non-live answers still re-execute (a replay must never hand back a dead handle, and a probe fault must never become a typed error about the RUN), but an unreachable store logs a WARNING naming the outage |
| 7 | `invocation_key` ignored `input_mode`, so a failed AUTO attempt could seed a `user_gated` replay - the gated run replaying steps computed from params it exists to revise | the RESOLVED gate mode is part of the key (`None` and `"auto"` hash the same, since they resolve the same) |
| 8 | the wave-1b ADR text | corrected above (blocker 2) |

## Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1583 passed, 5 skipped, 0 failed (baseline 0) |
| `tests/test_[f-o]*.py` | 6646 passed, 3 skipped, 1 xfailed, **4 failed** - all `test_fetch_resolution_gate.py` (baseline) |
| `tests/test_[p-r]*.py` | 2102 passed, 2 skipped, **2 failed** - both `test_run_river_dye_scenario.py` (baseline) |
| `tests/test_[s-z]*.py` | 1418 passed, 6 skipped, 0 failed (baseline 0) |
| `contracts/tests` | 721 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True` |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |
| live `telemac_do_sag` reference run (x2) | DO min 8.5772 mg/L @ 10631.7 m - parity with the wave-1 landing |

Exactly the 4 + 2 baseline failures. No `workers/` path touched, so no image
rebuild is in play.

## Live evidence

The reference question is unchanged (Eel River near Scotia, California; BOD 20,
20 C, standard 5, k1 0.3, k2 0.9, 12 km, mesh auto), with ONE difference forced
by the upstream: the NOAA National Water Model publishes only recent
`analysis_assim` cycles and has none for the current date, so the carrier
discharge cannot be fetched. The template refuses typed
(`TELEMAC_DISCHARGE_INPUT_REQUIRED`) and names the lever, which is the honest
path working - verified in isolation against `fetch_noaa_nwm_streamflow`, i.e.
upstream, not a regression. The reference run therefore PINS
`discharge_m3s=2.0`, the value wave 1 resolved from NWM, so the physics is
identical. `scripts/run_do_sag_direct.py` gained a `--discharge-m3s` flag for
exactly this.

**Parity** - two independent runs, bit-identical to the wave-1 landing:

| | wave 1 | wave 1c run A | wave 1c run B |
|---|---|---|---|
| run id | `01M0RA0RCXW4S40PN1RBSSPJ6M` | `01M0RKE53944J2TNCPYCADGB1X` | `01M0RN1FR6CY0A2Y572HFX271Q` |
| DO sag minimum | 8.5772 mg/L | 8.5772 mg/L | 8.5772 mg/L |
| sag location | 10631.7 m | 10631.7 m | 10631.7 m |
| violates the 5 mg/L standard | false | false | false |
| sag-curve points / first / last | 60 / 9.022 / 8.9623 | 60 / 9.022 / 8.9623 | 60 / 9.022 / 8.9623 |

**Tombstone, on the live store.** After run A the ledger collection holds ONE
document: `complete: true`, `records: []`, `schema_version: 3`. Run B is the
SAME question at the SAME params - the invocation key is identical - and it
RE-SOLVED (`executed=['do_field', 'do_field.chart:do_sag_curve'] replayed=[]`,
a fresh run id and a fresh solver container) rather than replaying run A. That
is the B1 ghost refuted end to end. After both runs the collection still holds
exactly one document, which is the accumulation bound in practice.

---

# Correction - wave 1d (third adversarial review)

Status: LANDED. A third adversarial verifier REFUTED the wave-1c landing on five
blockers and eight observations, all probe-proven. The theme is one sentence: a
form-gate REVISION was only half-honored. The sheet moved; the derivations, the
data fetched from the old values, and the ledger document the run moved away from
did not. As before, the sections above are left as written and this one records
the delta.

## Blocker 1 - a ParamRef could leak past the late-binding seam

`ParamRef` refused `bool()` and nothing else, so four operations turned a
DESCRIPTION into data, quietly:

| leak | what it did |
|---|---|
| `f"DO sag over {p.reach_km} km"` | baked the literal `ParamRef('reach_km')` into a layer title, which was then published and persisted |
| `str(ref)` / `"{}".format(ref)` | same, one call earlier |
| `p.alpha == 3.0` | answered `False` against the value the author meant - a silent wrong branch |
| `{p.label}` | a ref in a set, which the binder did not walk (the VALIDATOR did), so the runner received the description |

All four now refuse typed, naming `p.get(name)` and the late-binding contract.
`__repr__` deliberately stays live: naming the ref is exactly what a diagnostic is
for, and the leak guard's own message needs it.

Two structural fixes ride with it:

- **The binder and the validator agree on containers.** `_bind_value` gained
  `set`/`frozenset` arms. The validator had always walked sets for declared reads,
  so the two disagreed about what the plan said. Rebuilding a container also
  stopped assuming `type(x)(iterable)`: a namedtuple is rebuilt through `_make`,
  which keeps its field names.
- **A terminal leak guard.** Before any ledger record is persisted, before any
  runner is called with bound arguments, and before `interpret` returns, the
  interpreter scans for surviving `ParamRef` instances - recursively through
  mappings, sequences, sets and object `__dict__`s, under a node budget - and
  raises `ParamRefLeakedError` / `PARAM_REF_LEAKED` naming the path it sits at.
  A ref reaching disk is ALWAYS a bug, never data, so the honest answer is a typed
  refusal rather than a published lie about a number. The guard is a floor, not a
  deep-object crawler: `__slots__` objects are skipped (no cheap `__dict__`) and
  the scan stops after 50k nodes.

## Blocker 2 - a revision did not re-derive

The probe: `sat = 2 * temp`, temp revised 20 -> 30 at the form gate. The run
solved on `temp=30` and `sat=40` - a sheet contradicting itself, with the run's
own provenance asserting both.

`reseat_revised` is now followed by `rederive_revised`, which re-runs the
derivation fixpoint over the APPROVED sheet. Derived rows that consume a revised
value re-derive (60, with a note naming what was revised and by which resolver).

**The user always wins.** A row the user supplied or edited (`basis=user`) is
PINNED and never recomputed. Where the approved sheet would now derive something
else for a pinned row, the pin stands and the row's note SAYS so - "the sheet
approved at input review would derive 60, but this value was set explicitly and
stands". Silently overwriting an explicit edit is the same swallow this library
exists to outlaw; silently keeping it without saying why is the other half.

## Blocker 3 - a revision did not invalidate the data produced from the old values

A `Data` producer that ran lazily BEFORE the gate (because a pre-gate step
`Ref`ed it) fetched against the pre-review sheet, and the post-gate steps then
consumed it: terrain at 30 m surviving an approved 3 m, with the solve reading
`res=3` and `dem=dem@30`.

Dependency is knowable, because a producer's kwargs carry the `ParamRef`/`Ref`
reads it makes. On revision the interpreter evicts every `env.artifacts` entry
whose producer consumed a revised param - transitively through Data-to-Data
`Ref`s - and the eager batch (which fires at the first node after the last gate)
re-produces it against the approved sheet. Eviction is TARGETED: an artifact the
revision cannot have changed keeps its value, so this is not a blanket refetch.

The ledger side is subsumed by blocker 5: the old key's document, `data:` records
included, is reaped. The document at the RE-KEYED key is keyed by the approved
values, so anything in it was produced at those values and is legitimately
replayable.

## Blocker 4 - the law-9 floor was mode-weaker than the gate it replaced

Wave 1c re-homed the law-9 refusal for gateless plans but wrote it `auto`-only.
`gate_input_review` has always had TWO refusing arms - auto, and user_gated with
NO EMITTER - so a gateless plan in headless user_gated mode was the SOFTER path:
the caller asked for review, there was no session to review on, and the run
proceeded on an invented physics value anyway.

Restored, and it now mirrors the gate exactly: refuse in auto; refuse in
user_gated with no emitter ("no one to approve"); step aside only for a LIVE
user_gated session, where the card in front of the user is what owns the
approval.

Two more things closed with it:

- **The check no longer waits for a `consequential` flag** (observation 3). It
  fires before the FIRST step. An invented physics value poisons the prep work as
  surely as the solve, and a plan that tags nothing consequential skipped the
  floor entirely. Probe-proven: cases E (no consequential step) and F (a cheap
  prep step ahead of the solve) both ran to completion before; both now refuse
  with zero steps executed.
- **One error code** (observation 5). The gateless floor and the gate's own
  law-9 cancel both raise `PHYSICS_INPUT_REQUIRED`; only a non-law-9 cancel stays
  `INPUT_REVIEW_CANCELLED`. Callers route on the REASON, not on whether the plan
  happened to declare a form card. The gate's refusal TEXT also stopped telling a
  headless user_gated caller to "re-run in user_gated mode" - a lever they had
  already pulled; `physics_refusal_reason(..., no_session=True)` says what is
  actually true.

Observation 4, recorded as asked: **the prep steps that run before the door-6
refusal are by design.** `_refuse_missing_required` fires at the first
CONSEQUENTIAL step - the last honest moment - because a value can still arrive
from a gate or from a step that resolves it, and refusing earlier would fire
before the very thing that fills it. The law-9 floor is the opposite case (nothing
downstream can supply a real source for an invented default), which is why it
moved to the front.

## Blocker 5 - a re-key orphaned the key it moved away from

The re-key made the approved sheet a different invocation, correctly - but left
the ORIGINAL key's document on disk holding the records of a run that continued
somewhere else. Nobody can ever resume from it (no invocation will hash to those
values again with those results), and its records were computed from the very
values the review replaced. The probe's end state was two documents.

At re-key time the original document is now reaped. One run, one tombstone: the
probe's end state is a single `complete: true` document at the approved key.

## The observations

| # | what was wrong | what it is now |
|---|---|---|
| 1 | a gate could be the LAST node of a plan - nothing after it, so nothing its answer could change | the validator refuses it, alongside the existing gate-after-the-consequential-step rule. Both are the dead-gate rule |
| 2 | a plan could declare a FormGate AND branch (`When`) on a param that gate can revise. `When` is decided when the plan VALUE is built, i.e. before the review, so the branch was frozen against the pre-review sheet - the run would take one branch while its provenance claimed the other | REFUSED at validation. `ResolvedParams` records which names were read as CONCRETE values (`get`), which at validation time is exactly the plan's construction-time reads; a FormGate plus a `When` plus a read of any non-CONSTANT-door param is a shape that cannot honor a revision. Branching on a CONSTANT is still fine - it is not on the form as an editable value |
| 3 | folded into blocker 4 | |
| 4 | (the door-6 "last honest moment" question) | answered above, in blocker 4 |
| 5 | folded into blocker 4 | |
| 6 | `_bind` ran OUTSIDE the typed-error path, so a raw `TypeError` from rebuilding an author's container escaped the envelope every other plan fault arrives in | binding runs in the same typed path as `_call_runner`: retryable errors propagate, everything else becomes `StepFailedError` / `STEP_ARGS_UNBINDABLE` carrying the step label and the cause. The namedtuple case that motivated it is also FIXED rather than merely reported |
| 7 | the wave-1c text called the NWM outage standing | CORRECTED: it was TRANSIENT. `fetch_noaa_nwm_streamflow` serves `analysis_assim` again - verified in isolation and end to end (below) |
| 8 | no live evidence for the NWM resolution path itself | supplied below |

## Live evidence

**The NWM path, end to end.** One `telemac_do_sag` run with `discharge_m3s`
UNPINNED, so the carrier discharge resolves from the upstream that wave 1c could
not reach:

- `fetch_noaa_nwm_streamflow` downloaded
  `nwm.20260824/analysis_assim/nwm.t01z.analysis_assim.channel_rt.tm00.conus.nc`
  (resolved `valid_time=2026-08-24T01:00:00+00:00`), loaded 2,776,734 feature
  streamflow values, discovered 16 COMIDs in the reach bbox via NLDI and built 16
  feature rows (min 0.0000 / max 3.0600 / mean 1.0231 m3/s).
- `model_telemac_river_dye` resolved the **carrier discharge from the NOAA
  National Water Model, nearest reach to the seed** `-124.09228, 40.49559`. The
  value that reached the solver is **2.1 m3/s** - read back off the staged deck,
  `data/runs/01M0RRMQTSS21TXBSNJ42THTH9/t2d_river.cas`:
  `PRESCRIBED FLOWRATES = 2.1;0.0`.
- Gate provenance: the run's `discharge_m3s` row carries `value=2.1`,
  `units=m3/s`, **`basis=fetched`**,
  `real_source_if_any="fetch_noaa_nwm_streamflow (NOAA National Water Model)"`.
  A real source, so law 9 has nothing to refuse: `gate_input_review` (auto mode,
  at river_dye's own review) proceeded with the inputs labeled.
- Result: run `01M0RRMQTSS21TXBSNJ42THTH9`, DO sag minimum **8.5768 mg/L at
  10631.7 m**, does not violate the 5 mg/L standard, 60 curve points
  (first 9.022 / last 8.9635), layer
  `s3://trid3nt-runs/01M0RRMQTSS21TXBSNJ42THTH9/telemac_do_field.tif`,
  `executed=['do_field', 'do_field.chart:do_sag_curve'] replayed=[] notes=[]`.

That is observation 7 refuted at the source and observation 8's evidence gap
closed. It is recorded as the NWM-PATH proof, NOT a parity run: the upstream
resolved 2.1 m3/s where the reference pins 2.0, so the physics differs in the
fourth digit (8.5768 vs 8.5772; last point 8.9635 vs 8.9623). A different
discharge producing slightly different physics is the path WORKING.

**Parity, separately.** One pinned run (`--discharge-m3s 2.0`, logged as
`carrier discharge 2 m3/s (user-supplied)`) confirms the reference physics still
holds bit-for-bit:

| | wave 1 | wave 1c (A / B) | wave 1d pinned |
|---|---|---|---|
| run id | `01M0RA0RCXW4S40PN1RBSSPJ6M` | `01M0RKE53944J2TNCPYCADGB1X` / `01M0RN1FR6CY0A2Y572HFX271Q` | `01M0RT86QGPRFKEMDT9MZVC5PK` |
| DO sag minimum | 8.5772 mg/L | 8.5772 / 8.5772 | **8.5772 mg/L** |
| sag location | 10631.7 m | 10631.7 / 10631.7 | **10631.7 m** |
| violates the 5 mg/L standard | false | false / false | **false** |
| points / first / last | 60 / 9.022 / 8.9623 | same / same | **60 / 9.022 / 8.9623** |

## Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1604 passed, 5 skipped, 0 failed (baseline 0) |
| `tests/test_[f-o]*.py` | 6646 passed, 3 skipped, 1 xfailed, **4 failed** - all `test_fetch_resolution_gate.py` (baseline) |
| `tests/test_[p-r]*.py` | 2102 passed, 2 skipped, **2 failed** - both `test_run_river_dye_scenario.py` (baseline) |
| `tests/test_[s-z]*.py` | 1418 passed, 6 skipped, 0 failed (baseline 0) |
| `contracts/tests` | 721 passed (no delta) |
| `scripts/ws_smoke.py` | `all_passed=True`, case self-cleaned |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |
| live `telemac_do_sag` - NWM path, unpinned | ok, discharge 2.1 m3/s `basis=fetched`, DO min 8.5768 @ 10631.7 m |
| live `telemac_do_sag` - pinned 2.0 | ok, DO min 8.5772 @ 10631.7 m - parity with wave 1 and wave 1c |

Exactly the 4 + 2 baseline failures. No `workers/` path touched, so no image
rebuild is in play.

## Not in wave 1d

The wave-2 plugin form/draw cards, which is what would make the LIVE user_gated
arm of the law-9 floor (the one exemption the floor grants) a card a user can
actually answer. Until then the exemption is only reachable through a live
session that has no card to show, which is why the no-emitter arm is the one
that had to be restored.
