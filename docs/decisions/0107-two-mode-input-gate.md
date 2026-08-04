# ADR 0107 -- Two-mode INPUT_REQUIRED gate (review-before-run)

Status: accepted (2026-08-04, NATE-designed)
Follows: the template input-provenance audit
(`docs/validation/template-input-provenance-audit.md`) section 5a (the gate
proposal) + ADR 0106 (the structured `SyntheticInput` contract this gate
consumes + its Residuals naming this wave) + ADR 0099 (the mesh preview gate's
mode parameter, built expecting this lever) + ADR 0091/0102 (the `.suggestions`
typed-error recovery seam) + the #154 granularity gate spine
(`PayloadWarningEnvelopePayload` + `_PENDING_CONFIRMATIONS` block-and-wait +
`tool-payload-confirmation`).

## Context

ADR 0106 made input provenance a STRUCTURED field (`SyntheticInput`) that labels
a proceeding demo default but does NOT gate the sensitivity-dominant inputs. Its
Residuals handed this wave the gate: an `INPUT_REQUIRED` review that lets the
user look over / adjust the resolved input set before a solver runs. NATE's
design is a FEATURE with two run modes, unifying the already-live hard-pause
typed gates with a new soft review gate.

## Decision

### 1. The two run modes + the mode lever

A per-run `input_mode` param (`"auto"` | `"user_gated"`) on the wired simulation
templates + a session-level default env `TRID3NT_INPUT_GATE_MODE` (default
`auto`). `resolve_input_gate_mode(mode)` (`agent/gates/input_review.py`): explicit
param wins, else the session env, else `auto` -- so runs are NEVER blocked unless
the user opted in.

- **AUTO** (the shipped default): the run proceeds immediately; every non-user
  input is LOUDLY LABELED via the 0106 `synthetic_inputs` machinery (already
  carried on the result envelope). The review helper is a no-op pass-through.
- **USER_GATED**: AFTER the template RESOLVES its inputs (fetched values,
  prompt-interpreted, demo defaults) and BEFORE the solver dispatch, the resolved
  input set is presented for review/adjust; the run starts on approval, and the
  result stamps EXACTLY the reviewed entries so what-was-approved == what-ran.

The mesh preview gate (ADR 0099) threads from the SAME lever:
`mesh_gate_should_fire(paradigm, mode)` now fires for every paradigm when
`mode="user_gated"`, and a regular-grid mesh with `mode=None` consults the same
`TRID3NT_INPUT_GATE_MODE` session default (tin keeps its own signed-ON default).

### 2. The pause envelope (rides the #154 spine -- no new transport)

`gate_input_review(...)` is the shared helper the templates call after input
resolution. In `user_gated` mode it builds a `tool-payload-warning` carrying the
resolved `SyntheticInput` table (rendered into `recommendation` so the plugin's
EXISTING card surfaces it with NO new UI, plus a new ADDITIVE
`PayloadWarningEnvelopePayload.synthetic_inputs` field for the narration seam),
emits it through `current_emitter()`, registers a block-and-wait future in the
SAME `_PENDING_CONFIRMATIONS` registry the inbound `tool-payload-confirmation`
handler resolves, and awaits. Options `proceed | narrow_scope | cancel`:

- `proceed` -> run with the reviewed inputs, stamp the entries.
- `narrow_scope` == "provide values" -> merge `revised_args`, update the affected
  entries to `user` basis (optionally re-resolve via a callback), re-present.
  Bounded to 3 rounds, then an honest cancel.
- `cancel` -> a typed `USER_INPUT_CANCELLED`; the solver does not run.

The registry (`_PENDING_CONFIRMATIONS` + accessors) moved from `server` to the
leaf `agent/gates/pending.py` so an in-tool gate -- which cannot import `server`
at module load -- rides the same spine; `server` re-imports the names, so
`server._PENDING_CONFIRMATIONS` is still that dict (every existing gate test is
untouched). With NO live emitter (a headless direct-call / offline run) the gate
FAILS OPEN: it proceeds with the inputs labeled, never blocking a headless run.

### 3. The hard-ask half (unified, documented)

The existing per-template typed INPUT-required errors are the HARD-PAUSE half of
the feature and STAY as-is: `GEOCLAW_DAM_INPUT_REQUIRED`,
`LANDLAB_RAINFALL_INPUT_REQUIRED`, `TELEMAC_DISCHARGE_INPUT_REQUIRED`,
`SWMM_PRECIP_LOOKUP_FAILED`, `ASR` `USER_INPUT_REQUIRED`. When an input can
NEITHER be fetched NOR interpreted, the template raises its typed error with the
`.suggestions`-shaped message and the run stops in BOTH modes -- the review gate
is only reached once inputs ARE resolvable. This wave ADDS the gate-wave targets
0106 named:

- **OpenQuake Vs30**: the hardcoded 760 m/s rock value becomes a `vs30` tool
  param + a labeled `default_demo` `synthetic_inputs` entry, reviewable in
  `user_gated` mode. The value threads to the worker deck
  (`OpenQuakeRunArgs.reference_vs30_ms` -> `assemble_build_spec` ->
  `render_job_ini(reference_vs30_value=...)`, all ADDITIVE + default-matching, so
  the 760 default renders byte-for-byte). A Vs30 fetcher stays future.
- **SWAN wave-boundary params**: same treatment (labeled default + reviewable) --
  NOT wired this wave; queued on the adoption path below.

### 4. Narration (chat-tight, test-pinned)

The system prompt (`adapter.py`) gains a minimal review-envelope instruction:
present the resolved inputs as a compact list, ONE per line
(`param = value [basis, source]`), collect edits, confirm before running, do not
run until approved. Pinned by `test_system_prompt_has_input_review_instruction`.

### 5. Wired this wave

| tool | reviewed inputs | mode param |
|---|---|---|
| `geoclaw_inundation` | dam_break_depth_m, source_lonlat / source_magnitude, sim window, amr | yes |
| `modflow_asr` | injection/recovery rates, aquifer + cycle defaults | yes |
| `landlab_susceptibility` | triggering rainfall/recharge, demo soil block | yes |
| `telemac_river_dye` | carrier discharge (NWM/user), bank source | yes |
| `swmm_urban_flood` | rainfall depth (Atlas-14/user), synthesized network, overland Manning | yes |
| `openquake_psha` | reference Vs30 (760 rock default / user) | yes |

## Consequences

- ONE additive contract field (`PayloadWarningEnvelopePayload.synthetic_inputs`)
  + one additive `OpenQuakeRunArgs.reference_vs30_ms` (default-matching). No enum
  grown; registry UNCHANGED at 172; CODED tools unchanged (only params added to 7
  existing tools). Offline suite baseline preserved (9 by SET).
- The gate is ADDITIVE and layered ON TOP of the existing pre-dispatch
  solver-confirm / granularity / mesh gates (unchanged) -- those remain the
  Invariant-9 cost/mesh confirmation. In `user_gated` mode a solver may therefore
  show BOTH the existing cost card AND the input-review card; unifying them into
  ONE card is a deferred refinement (open issue).
- Flood/SFINCS seams UNTOUCHED (grep-verified: no edit to `flood.py` / sfincs
  builders); the canary is not required this wave.

## Open issues / adoption path

- Remaining 13 templates adopt the lever as they populate `synthetic_inputs`
  (0106 pattern): add `input_mode`, build provenance BEFORE dispatch, call
  `gate_input_review`, stamp `outcome.entries`. SWAN wave-boundary is next
  (0106-named).
- `provide values` re-resolution: the generic path updates the revised entry to
  user-basis without re-fetching; a per-template `reresolve` callback (wired for
  none yet) re-runs fetchers on revision (e.g. a revised dam name -> new NID
  lookup).
- The double-pause in `user_gated` (cost card + review card) should collapse to a
  single combined card in a later wave.
- Vs30 fetcher (USGS Vs30 map) + SWAN boundary fetchers stay future ("fetching
  stays future").
