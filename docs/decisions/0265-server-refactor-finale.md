# ADR 0265 - server-refactor finale: session state layer + turn wire plumbing

Status: LANDED (2026-08-14). Finale of the server-refactor series (ADR 0261 =
wave 1 package skeleton + errors/config; 0262 = wave 2 cloud-seam chop; 0263 =
wave 3 interactions/styles/spatial; 0264 = wave 4 reuse/dispatch/protocol).
Strictly behavior-preserving pure moves: the per-session STATE layer and the
turn WIRE plumbing extract out of `_core.py` into two sibling package modules.
Date: 2026-08-14
Supersedes-nothing (closes the server-refactor series; recon map at
`docs/design/server-refactor-recon-2026-08-14.md`, now carrying an END-STATE
section).

## Context

The NATE-agreed design for the finale: split STATE from BEHAVIOR. The recon
placed `SessionState` at "~4,700 lines" as "the core extraction problem." The
dominant finding on inspection corrects that framing: **the `SessionState`
dataclass is already thin** -- 237 lines of fields plus ONE lifecycle accessor
(the `active_case_id` property). The "~4,700 lines" the recon counted are the
module-level turn/dispatch functions that read a session; those already take
`state: SessionState` as their FIRST argument. State and behavior were never
fused at the language level -- they were co-located in one file. So the
finale's job is relocation, not a method->function rewrite.

That reframing also bounds what can move. The turn DRIVER (`_stream_model_reply`,
`_dispatch_model_turn_and_persist`) and the user-decision GATE coroutines are
behavior, not state, and they are entangled exactly where wave 4 flagged them:

- The gate-wait seam `_gate_wait_timeout` is SHARED. Its 5 call sites split
  across the payload-warning + solver-confirm gates (finale's nominal turn.py
  targets) AND the credential / region-choice / spatial-input emit-wait gates
  that wave 3 already ruled stay in `_core`. `test_gate_timeout_local`
  source-inspection-counts `_gate_wait_timeout(` >= 6 in `inspect.getsource(
  server._core)` (def + 5 calls). Moving only the 2 turn gates would leave
  NEITHER `_core` nor a new module with all 6 -- the seam is not cleanly
  separable, so the whole gate family stays in `_core`.
- The turn driver calls ~30 `_core`-resident persist/emit/dispatch helpers
  (`_persist_chat_turn`, `_finalize_segment`, `_invoke_tool_via_emitter`, the
  gates, the case/auth/resume machinery). Moving it while those stay would force
  a `_core` <-> `turn` import cycle -- a departure from the acyclic re-import
  pattern every prior wave preserved, under a behavior-preservation constraint.

Per the honest-partial-extraction rule and the explicit "a clean module beats a
broken zero" guidance: extract the cleanly-separable slices, leave the entangled
turn engine in `_core`, document the last mile.

## Decision

### Extractions

- `session.py` (384 LOC): the per-session STATE layer.
  - `SessionState` -- the `@dataclass` (fields + the `active_case_id` property,
    backed by the module registry). Moved verbatim.
  - Session-scoped registries the state is backed by, both keyed by
    `session_id` so every connection of a session shares one binding:
    active-Case (`_SESSION_ACTIVE_CASE`, `_SESSION_ACTIVE_CASE_CAP`,
    `_set_session_active_case`, sentinels `_CASE_SYNC_NEVER` / `_ROOT_STREAM_KEY`)
    and the anon-identity mirror (`_SESSION_ANON_ID`, `_SESSION_ANON_ID_CAP`,
    `_get_session_anon_id`, `_set_session_anon_id`, `_apply_session_anon_hint`).
  - Runtime deps imported directly (`AllowedToolSet`, `ToolCircuitBreaker` for
    the two `default_factory` fields; `AuthTokenEnvelope` for the anon-hint
    copy). Annotation-only types (`PipelineEmitter`, `PipelineStep`, `asyncio`)
    stay string annotations under `TYPE_CHECKING` -- no runtime import, no cycle.
- `turn.py` (425 LOC): the turn WIRE plumbing -- the leaf transport primitives
  every turn/gate/handler path emits through: `_new_envelope` (typed-Envelope
  build + Case-tag stamp), `_session_safe_send` (the mid-turn-survives-a-dead-
  socket fall-forward), `_send_error`, and the raw-JSON terminal frames
  `_send_loop_exhausted` / `_send_agent_abort` / `_emit_turn_complete` /
  `_emit_cache_status` plus the connection-liveness `_heartbeat_loop` +
  `HEARTBEAT_INTERVAL_SECONDS`. These are pure leaves: they reference only
  external contracts, the `.protocol` connection registry, and each other -- no
  `SessionState` behavior, no `_core` back-import -- so they extract as a unit
  with NO import cycle.

### The method->function transform inventory

EMPTY. `SessionState` carries zero behavior methods to convert; its only
non-field member is the `active_case_id` property, which stays a property
(attribute-accessed) in `session.py`. Every function that reads a session was
already a module-level `def f(state, ...)` -- they relocate as-is or stay in
`_core`, never transform. The recon's anticipated "former SessionState methods
become module functions" is a no-op because that separation predates this wave.

### The package facade + re-import mechanism (unchanged pattern)

`_core` re-imports all 20 moved names by name (`from .session import ...`,
`from .turn import ...`) so its bare-global references resolve unchanged. The
package facade's `__getattr__` resolves `trid3nt_server.server.<name>` ONLY
through `_core`, so every moved name MUST be re-imported into `_core` -- which
also keeps `from trid3nt_server.server import SessionState` and the `__all__`
entries (`SessionState`, `_send_loop_exhausted`) valid. `_EXTRACTION_MODULES`
gains `session` and `turn` so a monkeypatch write on
`server.<moved-name>` propagates to the owning sibling as well as `_core`.

### Reference sweep (wave-1 discipline: grep old paths to zero)

- Source-inspection anchors: unaffected. `test_gate_timeout_local` counts
  `_gate_wait_timeout` in `_core` -- the gates did NOT move. The `_core`-source
  anchors in `test_solver_confirm_gate` (`state.gate_decisions_this_turn = {}`)
  and the `_invoke_tool_via_emitter` / `_dispatch_model_turn_and_persist` /
  `run_server` getsource tests all target symbols that stay in `_core`.
- One external direct import: `interactions.py`'s `TYPE_CHECKING`
  `from ._core import SessionState` keeps resolving (the `_core` re-export). No
  test monkeypatches any moved name directly; all reach them via the facade.

### Flagged for a future pass (stays in `_core`)

The turn engine: `_stream_model_reply`, `_dispatch_model_turn_and_persist`, the
gate coroutines (`_maybe_gate_on_payload_warning`, `_gate_on_code_exec`,
`_gate_on_solver_confirm`, `_gate_with_turn_memory`), `FETCH_CONFIRM_TOOLS` /
`SOLVER_CONFIRM_TOOLS`, the `_gate_wait_timeout` seam, `_invoke_tool_via_emitter`
(+ its inline reuse-decision logic), and the emit/case/auth/resume/persist
machinery. Moving these needs the shared gate-wait seam untangled from the
credential/region/spatial gates AND a resolution of the driver<->helper cycle --
not a mechanical move. When that lands, the gates + driver join `turn.py`.

## Consequence

- `_core.py` = 11,074 lines (was 11,747; net -673 = 705 lines of defs moved out,
  +32 the two by-name re-import blocks). session.py 384 + turn.py 425.
- END-STATE module map (`server/`): `_core.py` 11,074 (the turn engine +
  connection loop), interactions 465, turn 425, session 384, spatial 265,
  dispatch 235, config 173, errors 147, protocol 122, styles 85, __init__ 83,
  reuse 43.
- What `_core` still holds and why: the turn engine (`_stream_model_reply`,
  `_dispatch_model_turn_and_persist`, `_invoke_tool_via_emitter`, all
  user-decision gates + the `_gate_wait_timeout` seam, the emit/case/auth/resume/
  persist helpers) and the WS connection loop (`_make_handler`, `run_server`,
  `inflight_turn_count`, `_LiveTurn` + the `_SESSION_LIVE_TURNS` detached-turn
  registry). The connection loop is inseparable from `SessionState` and the
  live-turn registry (protocol-owned but session-coupled, driven on every
  connect/disconnect/cancel); the turn engine is blocked as above. This is the
  "clean beats broken" end-state the finale scope allowed.
- Behavior preserved. GATES (finale close):
  - offline suite (four slices, baseline EXACTLY): `[a-e]` 1568 passed / 5
    skipped; `[f-o]` 6409 passed / 4 failed (baseline fetch_resolution) / 4
    skipped / 1 xfailed; `[p-r]` 2026 passed / 2 failed (baseline river_dye) /
    3 skipped; `[s-z]` 1449 passed / 6 skipped.
  - directly-affected + source-inspection tests (case-context-reset,
    gate-timeout-local, solver-confirm-gate, coastal-forcing-offloop,
    code-exec-tool) -- 48 passed UNCHANGED.
  - workflows import clean; TOOL_REGISTRY = 252; `server` imports OK; facade
    read + monkeypatch-write propagation + cross-boundary `is` identity verified.
  - daemon restart + `scripts/ws_smoke.py` -> `all_passed=True` (chat + geocode
    tool call through the restarted daemon; the moved heartbeat / turn-complete /
    error frames all flowed live through the image).
  - flood canary `scripts/run_sfincs_direct.py` -> status=ok, depth COG
    published, 7 frames + peak in MinIO.
- Registration-neutral: no `@register_tool` / spec change.
