# TRID3NT QGIS Plugin

The TRID3NT agent, docked inside QGIS: ask for data or a hazard simulation in
plain language, and the answer arrives as styled layers on the canvas you are
already working in.

This is **milestone 3 of v1** (per
`docs/design/qgis-plugin-product-analysis-2026-07.md`): on top of milestone
2's gate card + canvas AOI + reconnect + local Open-case-in-QGIS, this adds
remote-mode case export (artifact download), case switching from the Cases
dialog, a debounced case-list refresh, selected-polygon AOI, and honest
token-expiry handling.

## Layout

```
qgis-plugin/
  Makefile              zip / install / test targets
  README.md             this file
  trid3nt/              the plugin package (what gets zipped)
    __init__.py         classFactory entry point
    metadata.txt        QGIS plugin metadata (min 3.28, experimental)
    plugin.py           toolbar action + dock registration
    dock.py             chat dock (bubbles, status lines, settings dialog)
    ws_bridge.py        QThread worker bridging the client to Qt signals
    layers.py           agent layer events -> native QGIS layers
    plugin_settings.py  QSettings-backed settings (mode/URLs/token)
    trid3nt_client.py   PURE-PYTHON connection layer (no PyQGIS/PyQt imports)
    aoi.py              PURE canvas-AOI math (CRS transform, 2-deg guard)
    gate.py             PURE payload-warning gate logic (decision rules)
    case_export.py      PURE export-API client + exported-layer planning
    icon.svg
  tests/                connection-layer tests (no QGIS required)
```

## WebSocket library choice (documented decision)

The connection layer is a **minimal RFC 6455 client written on stdlib
sockets** (`trid3nt_client.WebSocketConnection`, ~200 lines: upgrade
handshake, masking, fragmentation, ping/pong, 16/64-bit lengths, TLS via
`ssl` for `wss://`). Why not a library:

- QGIS's bundled Python does not reliably ship one. On Debian/Ubuntu,
  `python3-qgis` depends on neither `websockets` nor `websocket-client`, and
  `python3-pyqt5.qtwebsockets` is a separate package QGIS does not require
  (verified against the apt dependency tree). OSGeo4W/Windows and macOS
  official builds differ again.
- The common plugin workaround -- vendoring `websocket-client` inside the
  plugin zip -- adds a third-party tree to maintain for a protocol surface we
  use a fraction of. QtWebSockets would tie the client to Qt and make it
  untestable outside QGIS, defeating the pure-python requirement.
- Zero dependencies keeps the zip pure-python (plugin repository no-binaries
  rule) and the client testable with any CPython.

Threading: the socket lives on a `QThread` worker (`ws_bridge.AgentBridge`);
Qt signals marshal events to the UI thread. Outbound sends are mutex-guarded
so the dock can send from the UI thread without blocking.

## Protocol

Speaks the agent's envelope protocol (see `vendor/web/src/ws.ts` and
`scripts/tool_routing_bench.py`):

- envelope: `{type, id (ULID), ts, session_id, case_id, payload}`
- handshake: `auth-token` -> `auth-ack`, `session-resume` -> `session-state`
- one case per QGIS session: `case-command {create}` -> `case-open`
- chat: `user-message` out; `agent-message-chunk` (narration),
  `pipeline-state` (tool steps -> status lines), `session-state`
  (`loaded_layers` -> QGIS layers), `turn-complete` in
- sim gate: `tool-payload-warning` in -> inline gate card ->
  `tool-payload-confirmation` out (`proceed` / `cancel` / `narrow_scope` +
  `revised_args`)
- reconnect: after a transport loss the worker re-dials with capped-jitter
  backoff (floor 1.5 s doubling to 5 s, jitter in [0.5, 1.0) x base -- the
  web client's exact ladder), reusing the SAME `session_id` + sticky
  anonymous user id; `session-resume` carries the active `case_id` so the
  server re-binds the case and replays its layers. Chat / cancel / gate
  confirmations issued while down buffer in a bounded queue (50, oldest
  dropped) and flush FIFO on resume. The first connect stays fail-fast.
- connection modes:
  - **local** (default): `ws://127.0.0.1:8765/ws`, anonymous (empty token)
  - **remote**: `wss://` URL + pasted bearer token; the token rides as the
    `?st=` query carrier (what the cloud broker authenticates before the
    upgrade) plus the in-band `auth-token` envelope. Token *acquisition*
    (Cognito sign-in) stays out of scope -- Settings explains where to copy
    one from (the web app session's `/ws?st=` request) and the dock detects
    expiry honestly: a failure that classifies as auth (the broker's
    pre-upgrade 401/403, or an in-band `AUTH_REQUIRED` error) STOPS the
    reconnect ladder and paints "token expired -- paste a fresh one in
    Settings" instead of silently retrying a dead token forever.

## Layer materialization

- **raster**: the agent publishes a ready TiTiler XYZ tile template
  (`.../{z}/{x}/{y}.png?url=...&rescale=...`) -> added as a QGIS XYZ raster
  layer (`type=xyz&url=...`).
- **vector, inline GeoJSON**: written to a temp `.geojson`, added as an ogr
  layer.
- **vector, `s3://` uri only**: translated to the local MinIO http form
  (`http://127.0.0.1:9000/<bucket>/<key>`, loaded via GDAL `/vsicurl/`) when
  mode=local; skipped with an honest status line in remote mode.
- Layers group under **"TRID3NT \<case\>"** in the layer tree and dedupe by
  `layer_id` (session-state snapshots replay the full list on every emit).

## Sim gate card (milestone 2)

Before a gated tool dispatch (large payload, or a solver run carrying the
#154 granularity suggestion) the agent pauses on a `tool-payload-warning`.
The dock renders it as an inline card: the tool name, the honest
estimated-vs-threshold MB numbers, the agent's recommendation, the
resolution ladder (rung combo with live ~cells / ~ETA recompute, suggested
rung preselected) and, when a `time_scale` rides along, editable
minutes-per-frame + window with a live frame-count readout. Buttons:

- **Proceed** with everything at the suggestions -> `decision="proceed"`.
- **Proceed** after overriding any value -> `decision="narrow_scope"` with
  `revised_args` carrying the changed values under the envelope's exact
  param keys (`resolution_param` / `cadence_param` / `duration_param`).
- **Cancel** -> `decision="cancel"`.
- Hard-cap warnings (no `"proceed"` in `options`) disable Proceed until a
  narrowing override is picked -- the agent would reject a bare proceed.

Sims never start without a click here (user-controlled granularity is a
standing product rule). The card locks after one answer.

## Canvas-extent + selected-polygon AOI (milestones 2-3)

The "Use map canvas as area of interest" toggle (default ON) makes "here"
mean the map you are looking at. The "Use selected polygon as AOI" toggle
(default OFF, milestone 3) overrides it: when the active layer has selected
features, the **bbox of the selection** (v1: the bounding box, not the exact
ring -- the agent's structured AOI carriers are 4-number boxes) is sent
instead, honestly labelled as a selection AOI in both the status line
(`AOI: selection 0.05 x 0.05 deg`) and the in-text context line. With no
selection resolved it falls back to the canvas extent; a too-large selection
is refused with an honest note, never silently swapped for the canvas.

- On connect, the session case is created with the canvas extent as
  `case-command create` `args.bbox = [lon_min, lat_min, lon_max, lat_max]`
  (EPSG:4326) -- the web app's #170 "AOI-first" carrier; the agent pins it
  as the Case AOI for every turn.
- Every outgoing message ALSO carries the CURRENT extent as an explicit
  bracketed context line in the message text (same `bbox = [...]` shape).
  The wire contract (`UserMessagePayload`, `extra="forbid"`) has no
  per-message bbox field, so text is the only per-turn carrier -- see
  `aoi.py` for the full rationale.
- A status line under the toggle shows what happened, e.g.
  `AOI: canvas 0.12 x 0.09 deg`.
- Guard: an extent over ~2 deg per side is NOT attached (the message goes
  out without an AOI and the status line says why) -- a whole-country canvas
  is not a usable simulation AOI.
- EPSG:4326 / EPSG:3857 canvases transform with pure math; any other project
  CRS falls back to `QgsCoordinateTransform`.

## Cases dialog: switch, refresh, open (milestones 2-3)

The **Cases** header button lists your cases (from the agent's `case-list`
envelope, received on connect). Three actions:

- **Open chat** (milestone 3): sends `case-command select`; the server
  replies with a full `case-open` rehydration and the dock REBINDS -- the
  header shows the case title, the layer group switches to
  "TRID3NT \<title\>" (dedup reset), and the case's persisted layers replay
  into it. The web-mirror rule applies: the local case stamp updates at send
  time so the next `session-resume` re-asserts the selected case even across
  a reconnect race.
- **Refresh** (milestone 3): the protocol has NO list-cases request verb --
  `case-list` only arrives as a server emission. Refresh is therefore one
  `session-resume` round trip (the exact frame the web client already sends
  as its ~25s keepalive; the server answers with `session-state` +
  `case-list`), debounced to one per 2 s. Documented tradeoff: a redundant
  session-state frame rides along; layer dedup by `layer_id` makes it a
  no-op.
- **Open in QGIS**: POSTs `{"case_id": ...}` to `/api/export-qgis` and ADDS
  the exported layers to your current project: every feature table from
  `export.gpkg` plus the GeoTIFFs, grouped under "TRID3NT export <case>".
  - **local mode**: the local agent's HTTP listener (default
    `http://127.0.0.1:8766`, configurable in Settings); artifact paths are
    read directly off disk.
  - **remote mode** (milestone 3): the same POST on the HTTP base derived
    from the remote WS URL (`wss://host/ws` -> `https://host`), then the
    `.gpkg`/`.qgz` artifacts download through
    `GET /api/export-qgis/file?path=<abs>` into a local temp dir. That route
    serves ONLY .qgz/.gpkg under the agent's export root (403 otherwise,
    404 when missing) -- so remote GeoTIFF rasters become an honest skipped
    note (view them via their published tile layers), and a 403/404 on one
    artifact surfaces verbatim without losing the rest.

Documented decision: we deliberately do NOT open the returned `project.qgz`
via `QgsProject.read()` -- that replaces your whole open project (unsaved
work, your layer tree, and the live chat-session group would be lost).
Adding layers is non-destructive; the `.qgz` path is still shown in a note
if you want the fully styled project.

## Install (by zip)

```bash
cd qgis-plugin
make zip        # builds trid3nt.zip
```

Then either QGIS > Plugins > Manage and Install Plugins > Install from ZIP,
or:

```bash
make install    # unzips into ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/trid3nt
```

Enable "TRID3NT" in the Plugin Manager (check "Show also Experimental
Plugins" under Settings -- metadata is experimental=True at this milestone).

## First run (LIVE smoke path)

1. Start the local TRID3NT stack (from the repo root):
   `make minio titiler` then `make agent` -- confirm
   `ws://127.0.0.1:8765/ws` is listening (`make status`). The plugin never
   starts or stops the stack; it only connects to it.
2. Install the plugin zip (above) and restart QGIS or reload the plugin.
3. Click the TRID3NT trident toolbar icon -- the chat dock opens on the right.
4. Press **Connect**. The status dot goes amber (connecting) then green, and
   the header shows the fresh case id.
5. Ask for a DEM:
   `Fetch a digital elevation model for a 5km box around Asheville, North Carolina.`
6. Watch the small status lines under the pending assistant bubble
   (`fetch_elevation - running` etc). When the layer event arrives, a
   "raster ... added (XYZ tiles)" note appears and the DEM lands in the layer
   tree under "TRID3NT \<case\>". Zoom to the layer to see tiles (served by
   the local TiTiler on :8080).
7. Typed errors (missing API key, failed fetch) surface as red status lines --
   never a spinner ending in silence.

## Tests (no QGIS required)

The connection layer is pure stdlib python; the tests need the `websockets`
package only for the stub agent server, which the trid3nt-local agent venv
already has:

```bash
cd qgis-plugin
make test
# or directly:
../venvs/agent/bin/python -m unittest discover -s tests -v
```

Covers (58 tests): envelope/ULID shape, anonymous + token handshakes
(`?st=` carrier verified on the upgrade path), case create (with and without
the AOI-first `args.bbox`), a full chat round trip (streamed chunks, pipeline
steps, layer-event parse incl. a >64 KiB inline-GeoJSON frame exercising the
64-bit length path), cancel, s3->MinIO translation, the QGIS XYZ uri builder,
gate-card decision rules + proceed/cancel/narrow_scope round trips against a
gated stub turn, the hard-cap path, the mirrored cells/ETA/frames estimate
math, canvas-AOI CRS math + the 2-deg guard + status/attach formatting,
the capped-jitter backoff ladder, a reconnect that re-binds the case and
flushes the bounded outbound queue, sticky-anonymous replay, case-list
parsing, and the export-API client + exported-layer planning.

Milestone 3 adds: the WS->HTTP base derivation, the remote artifact download
(200 attachment / 403 outside-root / 403 wrong-type / 404 missing, against a
stub that mirrors the agent's real route guards), the full remote
POST->download->plan round trip incl. per-file failure survival, case select
(wire shape + case-open rebind + resume re-assertion), null-rehydration and
defensive case-open parsing, the resume-refresh round trip + the pure
debouncer, selection-AOI precedence/status/context-line math, token-expiry
classification (pure + a stub round trip where a dead token gets
AUTH_REQUIRED + a 1008 close), and -- in a subprocess with the system
interpreter's PyQt5 -- the REAL `AgentBridge.start` Qt wiring (see below).

### The Qt-wiring regression test (why it exists)

Milestones 1-2 shipped a crash the stdlib tests could not see: the bridge's
`event = pyqtSignal(...)` SHADOWED the C++ virtual `QObject.event()`, so the
first QEvent Qt delivered (the ChildAdded from `QThread(self)` inside
`AgentBridge.start`) called the signal as an event handler -->
"TypeError: native Qt signal is not callable" --> qFatal, aborting all of
QGIS on the first Connect click. Fixed by renaming to `agent_event`;
`tests/test_milestone3.TestQtBridgeStart` now drives the real start wiring
under a `QCoreApplication` (subprocess; skips honestly when no PyQt5
interpreter exists). Rule: never name a pyqtSignal after a QObject virtual
(`event`, `eventFilter`, `timerEvent`, `childEvent`, ...).

`tests/headless_first_run.py` is the manual live-proof driver on top: it
loads the plugin inside a real offscreen `QgsApplication` with a fake iface,
connects, sends a prompt, waits for layers, and saves screenshots to
`docs/proof/`. Point it at the stub with
`TRID3NT_AGENT_URL=ws://127.0.0.1:<port>/ws` or at the live local stack
(default `ws://127.0.0.1:8765/ws`).

## Milestone 4 (next / toward installable beta)

- Live-QGIS pass over the new surfaces (case switch + remote export against
  the real cloud stack; the M3 proof ran against the stub).
- Remote-mode s3 vector layers in session-state (presigned fetch -- the
  export path now works remotely, the live layer path still skips).
- Token acquisition (Cognito sign-in) instead of paste-plus-help-text.
- Exact-ring selection AOI (v1 sends the bbox of the selection).
- Windows/macOS smoke pass (all testing so far is Linux).
