# server/ -- session, turn, dispatch, protocol

`trid3nt_server/server/` is the daemon core: the WebSocket connection loop, the
per-session state, the model-turn engine, and tool dispatch. It presents a
single-namespace facade so external importers see one `trid3nt_server.server.X`
surface. (See also `server-package.md` for the facade mechanism in detail.)

## Layout (ADR 0278 -- `_core.py` dissolved to zero)

- `session/` -- the bottom layer (imports nothing above it). `state.py`
  (SessionState + session-scoped registries), `persistence_ref.py` (the
  Persistence handle + env bootstrap), `case_state.py` (active-case
  persistence, AOI/geometry payload setters, case-layer records, the
  `_turn_case_id` / `_turn_case_bbox` readers).
- `dispatch/` -- tool execution. `emitter.py` (`_invoke_tool_via_emitter` +
  sync-offload safety + `_dispatch_tool_and_persist`), `results.py`
  (auto-publish, code-exec/chart emission), `persist.py` (chat/tool-card/chart
  persistence joins + per-turn narration registries), `aoi.py` (case-AOI
  pinning + default backfill), `helpers.py`, `reuse.py`.
- `turn/` -- the turn engine. `stream.py` (`_stream_model_reply` +
  `_dispatch_model_turn_and_persist`), `engine.py` (candidate emission,
  routing/stage labels), `cases.py` (case lifecycle over the wire),
  `live_turn.py` (the detached-turn registry), `wire.py` (envelope build +
  session-safe send primitives).
- `protocol/` -- the WS surface + the read-only HTTP catalog. `loop.py`
  (`_make_handler` + `run_server` + `inflight_turn_count`), `auth.py` (connect
  handshake + session resume), `handlers.py` (dev-invoke / secret-add /
  layer-delete + bg-task drain), `connections.py` (session-connection registry),
  `catalog_http.py` (the `/catalog` page + `/api/*` HTTP listener on 8766 --
  tool catalog, telemetry summary, case-list, building-detail, probe-point,
  ingest, local-models; mounted by `loop.run_server`). Its provider
  model-discovery is delegated to `adapters/model_discovery`; the corpus path
  is anchored on the package root, not `__file__` depth.
- Root leaves: `errors.py`, `config.py`, `styles.py`, `interactions.py`,
  `spatial.py`, and `__init__.py` (the facade).

## Composition

Imports flow one way -- `protocol -> turn -> dispatch -> gates -> session` --
with a per-module `logger` leaf below all. The turn driver (`turn/stream`) drives
`adapters/` for the model round-trip, `data/` for tool dispatch, `emission/` for
map frames, and `persistence/` for storage. The GateSpec confirm engine + the
five user-decision gate families are EVICTED to `trid3nt_server.gates.confirm`
(ADR 0278); the two server callers (`dispatch/emitter`, `turn/stream`) import the
gate functions function-locally to keep the `server <-> gates` package edge
acyclic.

## Invariants / extension points

- The facade keeps a single `trid3nt_server.server.X` namespace across the
  split: a read resolves to the first leaf that binds the name; a monkeypatch
  write propagates to every leaf that already defines it.
  `SOLVER_CONFIRM_TOOLS` / `FETCH_CONFIRM_TOOLS` synthesize from the registry
  via the gate engine.
- Behavior-preserving moves only: the `_core` dissolution relocated function
  bodies verbatim. `turn/stream.py` (2035 LOC) exceeds the 1500-line budget
  because its `_stream_model_reply` coroutine is 1828 lines -- splitting one
  function is a body refactor, deferred.
