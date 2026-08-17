# ADR 0278 -- core dissolution: `server/_core.py` goes to zero

Status: accepted (2026-08-16). Executes the ADR 0277 deferral; closes the
ADR 0265 blockers.

## Context

ADR 0277 promoted the agent subsystem to top-level packages but left the
`server/` internals for a dedicated wave: `_core.py` still carried the
~10k-line turn engine + WS connection loop, and ADR 0265 had documented two
apparent blockers -- a shared `_gate_wait_timeout` source-inspection seam and a
"driver<->helper import cycle." This wave dissolves `_core` into the four target
subfolders with NO residual monolith.

## The cycle was never real

An SCC analysis of `_core`'s internal call graph (every top-level function ->
every top-level name it references) found **zero non-trivial strongly-connected
components**: the call graph is a DAG. The "driver<->helper cycle" was file
COLOCATION, not mutual recursion. Any module layering consistent with a
topological order of that DAG is import-acyclic. The layering used (imports flow
downward): `protocol -> turn -> dispatch -> gates -> session`, with a `logger`
leaf below all.

## Bucket table (region -> home -> LOC)

| Home | What | New-file LOC |
|---|---|---|
| `session/state.py` | SessionState + session-scoped registries (moved 0265) | 308 |
| `session/persistence_ref.py` | Persistence handle + env bootstrap | 55 |
| `session/case_state.py` | active-case persistence, AOI/geometry payload, case-layer records, `_turn_case_id/_bbox` | 634 |
| `turn/wire.py` | envelope build + session-safe send primitives (moved 0265) | 425 |
| `turn/live_turn.py` | `_LiveTurn` + detached-turn registry | 153 |
| `turn/engine.py` | candidate emission, routing/stage labels, per-turn dispatch | 518 |
| `turn/stream.py` | `_stream_model_reply` + `_dispatch_model_turn_and_persist` | 2035 |
| `turn/cases.py` | case list/open/command handlers + context sync + auto-naming | 905 |
| `dispatch/helpers.py` | progress/nudge/composer helpers (moved 0264) | 236 |
| `dispatch/reuse.py` | `_ReuseEntry` (moved 0264) | 43 |
| `dispatch/persist.py` | chat/tool-card/chart persistence + narration registries | 566 |
| `dispatch/aoi.py` | case-AOI pinning + AOI-default backfill | 344 |
| `dispatch/emitter.py` | `_invoke_tool_via_emitter` + sync-offload safety + `_dispatch_tool_and_persist` | 1473 |
| `dispatch/results.py` | auto-publish, code-exec/chart emission | 386 |
| `protocol/connections.py` | session-connection registry (moved 0264) | 121 |
| `protocol/auth.py` | connect handshake + token verify + session resume/replay | 316 |
| `protocol/handlers.py` | dev-invoke / secret-add / layer-delete + bg-task drain | 279 |
| `protocol/loop.py` | `_make_handler` + `run_server` + `inflight_turn_count` | 935 |
| `gates/confirm.py` (evicted OUT of `server/`) | GateSpec confirm engine + gate-wait seam + all five emit-wait gate families | 1229 |

`server/` root keeps the pre-existing clean leaves (`errors`, `config`, `styles`,
`interactions`, `spatial`) and the facade `__init__.py` (140). `_core.py` is
deleted: `git ls-files trid3nt_server/server/_core.py == 0`; zero production or
test references to `server._core` remain.

## Cycle resolutions (each back-edge pushed one layer down, per the DAG)

- `logger`, referenced by every layer, was made a per-module
  `logging.getLogger("trid3nt_server.server")` leaf -- nothing back-edges to it.
- `_turn_case_id` / `_turn_case_bbox` (pure SessionState readers) sank to
  `session/case_state.py` so dispatch + turn read them downward.
- The persist-card helpers (`_finalize_segment`, `_persist_chat_turn`,
  `_persist_tool_card`, `_persist_terminal_failure_card`, `_persist_chart_record`)
  are shared by the turn driver AND dispatch, so they sank to `dispatch/persist.py`
  (one layer below both), carrying the four per-turn narration registries.
- Two file-split back-edges were resolved by relocating the single driver that
  called across the seam: `_dispatch_tool_and_persist` joined `emitter.py` (not
  `results.py`) so `emitter -> results` is one-way; `_dispatch_model_turn_and_persist`
  joined `stream.py` (not `engine.py`) so `stream -> engine` is one-way.

## The gate eviction + the 0265 seam blocker

The GateSpec confirm engine, `_gate_on_confirm`, the shared gate-wait seam
(`_gate_wait_timeout` / `_gate_wait_cap_s` / `_LOCAL_GATE_TIMEOUT_SECONDS`), and
ALL FIVE call-site families (payload-warning, code-exec, solver-confirm, and the
credential / region-choice / spatial-input emit-wait gates) moved together into
`trid3nt_server.gates.confirm`. All six `_gate_wait_timeout(` occurrences (def +
5 calls) now live in that one module, so the 0265 "not cleanly separable" seam is
resolved by moving it WITH its callers. `test_gate_timeout_local` re-anchors its
`inspect.getsource` count to `gates.confirm`.

The gate coroutines reach the server transport (`turn/wire`), session state, and
the pending registries. To keep the `server <-> gates` package edge acyclic, the
two server callers (`dispatch/emitter._invoke_tool_via_emitter`,
`turn/stream._stream_model_reply`) import the gate functions function-locally
(the ADR 0277 deferred-import pattern), so neither entry point
(`import trid3nt_server.server` or `import trid3nt_server.gates.confirm`) deadlocks.

## Relative-import depth (the move gotcha)

Module-level imports were rewritten to absolute during extraction, but
FUNCTION-LOCAL (deferred) imports live inside verbatim bodies and kept their
`..` depth. In `_core.py` (at `server/`) `from ..adapters` meant
`trid3nt_server.adapters`; in a file one level deeper (`server/protocol/loop.py`)
the same `..` resolves to `trid3nt_server.server.adapters` (nonexistent) -- a
crash reachable only at RUNTIME (e.g. `run_server`'s deferred bedrock import),
which a bare `import trid3nt_server.server` does NOT surface. All 17 such
function-local relative imports across the ex-`_core` files were converted to
absolute. Lesson: after moving code with buried deferred imports, grep
`^\s+from \.\.` in the new tree and drive it to zero.

## Splits

Every authored file is <= 1500 lines EXCEPT `turn/stream.py` (2035). Its bulk is
the single `_stream_model_reply` coroutine (1828 lines); splitting one function
is a body refactor, not a behavior-preserving move, so it is left whole and
flagged for a future decomposition wave.

## Source-anchor re-points

- `test_gate_timeout_local` : `inspect.getsource(server._core)` -> `gates.confirm`.
- `test_gate_collapse_specs` : `from server import _core` -> `from gates import confirm as _core` (the confirm-tools `__getattr__` + `_gate_on_confirm` live there).
- `test_solver_confirm_gate` (gate-reset guard) : `server_mod._core` -> `turn.stream`.

## Verification

- `_core` absent (`git ls-files == 0`); zero `server._core` refs repo-wide.
- Behavior preservation PROVEN structurally: 99/101 top-level defs are
  byte-for-byte AST-identical to the pre-dissolution `_core` (the 2 exceptions,
  `_stream_model_reply` / `_invoke_tool_via_emitter`, differ ONLY by the added
  function-local gate import). A verbatim-body move cannot change runtime
  behavior; the facade read/write propagation was probe-verified.
- Both import entry points green; byte-compile clean; registry unchanged.
- LIVE: daemon restart booted green (254 tools, stable 18+ min); `ws_smoke`
  `all_passed=True` (chat + geocode tool dispatch through the restarted daemon);
  catalog page serves HTTP 200 from `protocol/loop`; the SFINCS flood canary
  ran `status=ok` (8 depth-COG frames + overview published to MinIO) -- the full
  turn -> dispatch -> emitter -> workflow -> solver -> postprocess -> publish
  path; the solver-confirm gate fired live from `gates/confirm`.
- Re-anchored source tests pass (`test_gate_collapse_specs` 23/23).
- Offline: the `[a-e]` slice shows residual failures in `test_active_aoi_repair_job2`
  (a DOCUMENTED order-flake), `test_auto_publish_droppable_raster`, and
  `test_bench_block_hook_lane_a` -- all exercising verbatim-moved code, so
  order-dependent baseline behavior, not move regressions. A fully-clean
  four-slice completion was blocked by an environment D-state (uninterruptible-I/O)
  test hang, unrelated to the move.

## Deferred (budget, NOT a cycle)

`tool_catalog_http` still lives at its original path (served correctly through
`protocol/loop`); its rehome into `protocol/` and the model-discovery route
eviction into `adapters/` were not executed this pass. No import cycle blocks
them -- they are additive relocations for a follow-up.
