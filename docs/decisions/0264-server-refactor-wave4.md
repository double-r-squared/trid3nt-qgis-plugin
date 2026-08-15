# ADR 0264 - server-refactor wave 4: reuse shim / dispatch helpers / connection registry

Status: LANDED (2026-08-14). Wave 4 of the server-refactor series (ADR 0261 =
wave 1 package skeleton + errors/config; ADR 0262 = wave 2 cloud-seam chop; ADR
0263 = wave 3 interactions/styles/spatial). Strictly behavior-preserving pure
moves: three more low-coupling regions extract out of `_core.py` into sibling
package modules, and the package facade extends its write-propagation set to the
new modules.
Date: 2026-08-14
Supersedes-nothing (continues ADR 0261/0262/0263; recon map at
`docs/design/server-refactor-recon-2026-08-14.md`).

## Context

Wave 4's named targets were the reuse cache, the tool-dispatch machinery, and
the WS-handler/serve tail. The dominant finding on inspection: the *heart* of
each region is entangled with machinery this wave does not own (the giant
`_invoke_tool_via_emitter`, `SessionState`, the `_SESSION_LIVE_TURNS` registry --
the session/turn wave's surface) or with `_core`-resident send/envelope plumbing.
Per the honest-partial-extraction rule (move what is clean, flag the rest), each
region contributes only its cleanly-separable, session-free slice; the coupled
remainder is flagged below for the session/turn wave.

## Decision

### Extractions

- `reuse.py` (1 symbol, 43 LOC): `_ReuseEntry` -- the drop-in
  `RegisteredTool`-shaped shim the scenario/fetch reuse short-circuit swaps into
  the registry so the same `emit_tool_call` LayerURI gate fires with the reused
  layer. A pure `@dataclass`; `LayerURI` is only a string annotation
  (`from __future__ import annotations` + `TYPE_CHECKING`), no runtime import.
- `dispatch.py` (12 symbols, 235 LOC): the low-coupling, session-free dispatch
  helpers.
  - loop-watchdog progress witness: `_PROGRESS_RESULT_KEYS`,
    `_dispatch_made_progress`.
  - post-deliverable / empty-completion / discovery knobs:
    `_POST_DELIVERABLE_WRAPUP_ROUNDS`, `_DELIVERABLE_COMPLETE_DIRECTIVE`,
    `_EMPTY_COMPLETION_RETRY_CAP`, `_EMPTY_COMPLETION_NUDGE`,
    `_DISCOVERY_EXPAND_CAP`.
  - tool-search + gate-expander name sets: `_tool_search_tool_names`,
    `_default_declarable_registry`, `_gate_expander_tool_names`,
    `_tool_names_from_search_result`.
  - terminal-composer classifier: `_is_terminal_composer`.
  - Deps are all external (`TOOL_REGISTRY`, `LayerURI`, `logger`) -- no
    `SessionState`, no `_core` back-import.
- `protocol.py` (6 symbols, 122 LOC): the session-connection registry -- the
  cleanly-separable slice of the daemon connection plumbing.
  `SESSION_SUPERSEDED_CLOSE_CODE`, `_SESSION_WS_CONNECTIONS`,
  `_register_session_connection`, `_deregister_session_connection`,
  `session_connection_count`, `_reap_prior_session_connections`. Self-contained
  (reads only the module-local dict + a logger); a clean leaf with no `_core`
  back-import.

Each module carries `from __future__ import annotations`, a
`logging.getLogger("trid3nt_server.server")` matching `_core` (same singleton by
name), and imports its external deps directly. `_core` re-imports all 19 moved
names by name (the wave-1/3 pattern) so its bare-global references and the
facade-proxied reads resolve unchanged.

### The facade extension

`_ServerFacade.__setattr__`/`__delattr__` already propagate a monkeypatch write
to `_core` plus any sibling extraction module whose `__dict__` defines the name
(ADR 0263). Wave 4 adds `reuse`, `dispatch`, `protocol` to `_EXTRACTION_MODULES`.
Because `_core` re-imports the moved names, both `_core` and the owning sibling
carry the binding in their `__dict__`; a test patching
`server._dispatch_made_progress` / `server._SESSION_WS_CONNECTIONS` reaches every
namespace that reads it, with zero test changes.

### Flagged entanglements (stay in `_core`, for the session/turn wave)

- Reuse DECISION logic: the scenario-index + fetched-layer lookups and every
  short-circuit branch live inline in `_invoke_tool_via_emitter`, woven through
  the dispatch loop and `SessionState`. Only the `_ReuseEntry` shim is
  separable this wave.
- The user-decision gate coroutines -- `_maybe_gate_on_payload_warning`,
  `_gate_on_code_exec`, `_gate_on_solver_confirm`, `_gate_with_turn_memory` --
  emit on the websocket via `_core`'s send/envelope plumbing (`_new_envelope`,
  `_send_error`, `_session_safe_send`, which stay in `_core`) and read
  `SessionState` audit/decision fields. Moving them would require a
  `_core`<->`dispatch` import cycle AND would strip the `_gate_wait_timeout(`
  call sites the source-inspection guard counts (below). They move when the
  send-plumbing relocates.
- `_gate_wait_timeout` / `_gate_wait_cap_s` / `_LOCAL_GATE_TIMEOUT_SECONDS`: the
  gate-wait seam, PINNED to `_core` by
  `test_gate_timeout_local.test_every_gate_wait_site_uses_the_seam`, which asserts
  `inspect.getsource(server._core).count("_gate_wait_timeout(") >= 6` (def + 5
  call sites). Kept in `_core` with the gate coroutines it serves.
- `FETCH_CONFIRM_TOOLS` / `SOLVER_CONFIRM_TOOLS`: the solver/fetch gate-tool name
  sets, consumed by the flagged gate coroutines -- stay paired with them.
- `_make_handler` (the WS connection loop) + `run_server` + `inflight_turn_count`:
  the handler is inseparable from `SessionState` (`state.turn_count`,
  `state.routing_mode`, `state.current_turn_*`, `_prepare_user_turn`, the
  `_handle_*` handlers) and the `_SESSION_LIVE_TURNS` registry -- the session/turn
  wave's owned surface. `run_server` still resolves through the facade for
  `main.py`; `inflight_turn_count` reads `_SESSION_LIVE_TURNS`.

## Consequence

- `_core.py` = 11,747 lines (was 12,041; net -294 = 324 moved out, +30 the three
  by-name re-import blocks). reuse 43 + dispatch 235 + protocol 122.
- Behavior preserved: full four-slice offline suite at the documented baseline
  (EXACTLY 4 `[f-o]` fetch_resolution + 2 `[p-r]` river_dye, all else green);
  the directly-affected + source-inspection tests
  (`test_gate_timeout_local`, `test_crisp_end_after_deliverable`,
  `test_discovery_expands_gate_lane_a`, `test_session_durability_jobs_bc`,
  `test_fetch_reuse_dispatch_f96`, `test_unique_layer_id_mint_f97`,
  `test_solver_confirm_gate`, `test_tool_gating_stage3`) pass UNCHANGED.
- Registration-neutral: no `@register_tool` / spec change; TOOL_REGISTRY = 252.
- GATES (wave close):
  - offline suite: `[a-e]` 1568 passed / 5 skipped; `[f-o]` 6409 passed / 4
    failed (baseline fetch_resolution) / 4 skipped / 1 xfailed; `[p-r]` 2026
    passed / 2 failed (baseline river_dye) / 3 skipped; `[s-z]` 1449 passed / 6
    skipped.
  - workflows + registry import clean (TOOL_REGISTRY = 252; `server` imports OK).
  - daemon restart + `scripts/ws_smoke.py` -> `all_passed=True` (chat + geocode
    tool call through the restarted daemon on the extracted code).
  - flood canary `scripts/run_sfincs_direct.py` -> status=ok, depth COG
    published, envelope + frames/peak in MinIO.
