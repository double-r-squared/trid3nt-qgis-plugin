# server/ -- session, turn, dispatch, protocol

`trid3nt_server/server/` is the daemon core: the WebSocket connection loop, the
per-session state, the model-turn engine, and tool dispatch. It presents a
single-namespace facade so external importers see one `trid3nt_server.server.X`
surface. (See also `server-package.md` for the facade mechanism in detail.)

## What lives here

- `_core.py` (~10k LOC) -- the turn ENGINE (`_stream_model_reply`,
  `_dispatch_model_turn_and_persist`, `_invoke_tool_via_emitter`, the
  user-decision gate waits + the `_gate_wait_timeout` source-inspection seam)
  AND the WS connection loop (`_make_handler`, `run_server`, `_LiveTurn` +
  the detached-turn registry). It imports every extraction sibling below.
- Extraction siblings (concerns already lifted out of the monolith):
  `session.py` (SessionState + session-scoped registries), `turn.py` (turn
  wire plumbing + envelope build), `dispatch.py` (session-free dispatch
  helpers), `protocol.py` (session-connection registry), `reuse.py`
  (`_ReuseEntry`), `interactions.py`, `spatial.py`, `styles.py`, `config.py`,
  `errors.py`.
- `__init__.py` -- the facade: read-proxy over `_core` + monkeypatch-write
  propagation to `_EXTRACTION_MODULES`.

## Composition

`_core` drives `adapters/` for the model round-trip, `data/` for tool
dispatch, `emission/` for map frames, and `persistence/` for case/chat/session
storage. External code (`main`, `telemetry`, `tool_catalog_http`,
`cases.ingest_user_layer`) imports through the facade; imports flow one way
(turn -> dispatch -> data), never back.

## Invariants / extension points

- The facade keeps a single `trid3nt_server.server.X` namespace across the
  split; monkeypatch writes propagate to the owning extraction module.
- Deferred (ADR 0277, inheriting the ADR 0265 blockers): the target
  `session/ turn/ dispatch/ protocol/` SUBFOLDER split and the extraction of
  `_core`'s turn-engine + WS-loop bodies into them, plus the GateSpec engine ->
  `gates/` and `tool_catalog_http` -> `server/protocol/`. These remain blocked
  by the shared gate-wait seam + the driver<->helper cycle and warrant their
  own gated wave (ws_smoke + flood canary through a restarted daemon).
