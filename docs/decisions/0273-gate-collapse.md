# ADR 0273 - Confirm-gate collapse: declarative GateSpec, one generic engine

## Context

The solver / fetch confirmation gate was hand-wired in `server/_core.py`:

- two name-set LITERALS -- `SOLVER_CONFIRM_TOOLS` (a paragraph-comment per
  entry) and `FETCH_CONFIRM_TOOLS` -- were the membership test;
- `_gate_on_solver_confirm` carried SEVEN per-engine locals (`swmm_autoscale`,
  `fetch_suggestion`, `flood_output_interval_min`, `flood_cadence_gated`,
  `telemac_preview`, `flood_grid_autoscale`, `flood_duration_hr`), a per-engine
  `if/elif` card-building chain, and a per-engine decision-tail branch;
- the four proceed/cancel card builders (`_build_fire/geoclaw/psha/scenario_
  confirm_envelope`) were imported into `_core` and dispatched by tool name.

Every new gated engine meant editing three places in `_core`. NATE's design
call: build the gate from tool METADATA, not hand-wired code -- the precedent is
`ResolutionSpec` (declarative metadata on the registration decorator, machinery
renders/enforces uniformly) and `gate_input_review` (a generic callable, engine
knowledge stays in the engine).

## Decision

**The GateSpec contract** (`trid3nt_contracts.gate_spec`, attached to
`AtomicToolMetadata.gate_spec`, additive default `None` -- the ResolutionSpec
shape):

- `kind`: `"solver"` | `"fetch"`. A solver strips a model-supplied `confirmed`
  before gating and injects it only on proceed; a fetch does not.
- `estimate_provider`: dotted import path to a pure `(params) -> CardEstimate`
  builder exported from the tool's module (async providers are awaited -- the
  TELEMAC mesh preview / SWMM real-cap re-probe).
- `pin_provider`: dotted path to `(decision, revised_args, params, tail_state)
  -> delta` -- the engine's own decision-tail arithmetic. `None` for a plain
  proceed/cancel gate (the generic tail then injects `confirmed` for a solver).
- `levers`: declared `LeverSpec` list (`name`, `param`, `unit`, `rungs`-or-
  `range`, `pin_on_proceed`). A gate declaring levers MUST name a pin provider
  (validator: a lever with no pin is a dead knob).
- `title` / `rationale`: card-copy metadata.

`CardEstimate` (runtime, server-side `agent/gates/cards/estimate.py`) carries the
built `PayloadWarningEnvelopePayload` (its `granularity` / `time_scale` blocks are
the generic surfaces -- cells/nodes, MB, runtime, ladder rungs, preview stats) +
the opaque `tail_state` the pin provider reads. `envelope=None` signals "no gate
needed" (the fetch_landcover no-coarsening skip).

**One generic engine** `_gate_on_confirm(ws, state, tool, params, gate_spec)`:
membership = `_gate_spec_for(tool)` (metadata lookup, the name sets die); card =
the declared estimate provider; decision tail = the declared pin provider (levers
pinned on proceed / honoured clamped on narrow_scope) or, lever-less, a plain
`confirmed` inject. Fail-open preserved EXACTLY (estimate failure falls through;
`envelope=None` dispatches as-is; headless timeout / cancel fail closed). The
wire envelope is UNCHANGED, so the plugin needs nothing. `_gate_on_solver_confirm`
survives as a thin compat shim (resolve spec -> delegate) so the gate-behavior
suite drives it unchanged; `SOLVER_CONFIRM_TOOLS` / `FETCH_CONFIRM_TOOLS` survive
ONLY as registry-DERIVED views (`_core.__getattr__`) -- the source of truth is the
specs.

## Migration table

| engine (tool) | kind | estimate provider | pin provider | levers | equivalence verdict |
|---|---|---|---|---|---|
| sfincs_flood | solver | `estimate_flood_run_settings` | `pin_flood_run_settings` | grid_resolution_m, output_interval_min, duration_hr | tail relocated verbatim; gate suite green |
| swmm_urban_flood | solver | `estimate_swmm_granularity` | `pin_swmm_granularity` (async real-cap re-probe) | target_resolution_m | tail relocated verbatim; granularity suite green |
| telemac_river_dye | solver | `estimate_telemac_mesh` (async, emits preview) | `pin_telemac_mesh` (seed-pair decouple) | mesh_resolution_m | tail relocated verbatim; mesh-preview suite green |
| fetch_dem / topobathy / landcover | fetch | `estimate_fetch_resolution` (landcover skip) | `pin_fetch_resolution` (floor-clamp) | resolution_m | tail relocated verbatim; fetch-resolution suite at baseline |
| openquake_psha | solver | `estimate_psha` | (none) | - | byte-identical envelope (dump-minus-warning_id test) |
| openquake_scenario_gmf | solver | `estimate_scenario` | (none) | - | byte-identical envelope test |
| openquake_secondary_perils | solver | `estimate_scenario` | (none) | - | byte-identical envelope test |
| elmfire_fire_spread | solver | `estimate_fire` | (none) | - | byte-identical envelope test |
| geoclaw_inundation / tsunami_gauge_timeseries | solver | `estimate_geoclaw` | (none) | - | byte-identical envelope test |

The estimate/pin providers WRAP the pre-collapse builder + decision-tail code
unchanged (relocated, not rewritten), so the card payload + pinned params are
byte-identical by construction. The four pure-arithmetic engines carry an explicit
`model_dump()`-minus-`warning_id` equivalence assertion
(`tests/test_gate_collapse_specs.py`); the lever-bearing engines' equivalence is
proven by the existing gate suites (which assert exact card fields + pinned params)
passing UNCHANGED.

## Deleted / added LOC

- `_core.py`: removed the two name-set literals (~85 lines incl. comments), the
  seven locals + `if/elif` card chain + per-engine tail branches (`_gate_on_
  solver_confirm` body ~516 lines), and 11 builder/clamp imports. Added the
  generic `_gate_on_confirm` + thin shim + `_gate_spec_for` /
  `_confirm_tools_by_kind` / `__getattr__` (~210 lines). Net `_core` ~ -390 lines.
- `solver_confirm.py`: +~260 lines (estimate/pin providers wrapping the existing
  builders + relocated tail arithmetic). Builders themselves unchanged.
- `gate_spec.py` (new contract, ~185), `estimate.py` (new runtime, ~95).
- Net across the change: roughly LOC-neutral, but the per-engine dispatch/tail
  surface in `_core` collapses to one generic path + declarative metadata.

## What resisted the collapse (flagged honestly)

- The per-engine card BUILDERS + decision-tail arithmetic did NOT genericize into
  "one rule over CardEstimate + levers": the SWMM real-build cap re-probe, the
  TELEMAC seed-pair decouple, the fetch finest-allowed floor-clamp, and the flood
  dual-lever (resolution + cadence + window) are irreducibly engine-specific.
  They are relocated intact into per-engine pin providers (engine knowledge stays
  in the engine, the gate_input_review precedent) rather than folded into the
  generic tail. This is the honest boundary: the MEMBERSHIP + DISPATCH + wiring
  collapse to metadata; the engine-specific pinning stays engine-owned.
- The builders currently live in the shared `agent/gates/cards/solver_confirm.py`
  (the pure card layer) with the providers, not physically moved into each
  workflow module. The GateSpec dotted paths point at that shared module. A
  follow-up mechanical relocation into each workflow module is possible but was
  scoped OUT to avoid import-cycle risk against the byte-equivalence bar.
- `SOLVER_CONFIRM_TOOLS` / `FETCH_CONFIRM_TOOLS` survive as registry-derived
  views (not deleted symbols) so the ~15 membership-assertion tests + any external
  reader keep working; the hand-wired LITERALS (the mess NATE called out) are gone.
