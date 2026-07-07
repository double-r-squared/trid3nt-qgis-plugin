# TRID3NT QGIS Plugin

The TRID3NT agent, docked inside QGIS: ask for data or a hazard simulation in
plain language, and the answer arrives as styled layers on the canvas you are
already working in.

This is **milestone 2 of v1** (per
`docs/design/qgis-plugin-product-analysis-2026-07.md`): on top of milestone
1's skeleton + connection layer + chat dock + layer materialization, this
adds the simulation gate card, canvas-extent AOI, capped-jitter reconnect
with queued sends, and Open-case-in-QGIS.

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
    (Cognito sign-in) is out of scope in milestone 1 -- paste one into
    Settings.

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

## Canvas-extent AOI (milestone 2)

The "Use map canvas as area of interest" toggle (default ON) makes "here"
mean the map you are looking at:

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

## Open case in QGIS (milestone 2)

The **Cases** header button lists your cases (from the agent's `case-list`
envelope, received on connect). "Open in QGIS" (local mode) POSTs
`{"case_id": ...}` to the local agent's `/api/export-qgis` (HTTP listener,
default `http://127.0.0.1:8766`, configurable in Settings) and then ADDS the
exported layers to your current project: every feature table from
`export.gpkg` plus every GeoTIFF in the export folder, grouped under
"TRID3NT export <case>".

Documented decision: we deliberately do NOT open the returned `project.qgz`
via `QgsProject.read()` -- that replaces your whole open project (unsaved
work, your layer tree, and the live chat-session group would be lost).
Adding layers is non-destructive; the `.qgz` path is still shown in a note
if you want the fully styled project. Remote mode gets an honest
"local-mode only for now" note (presigned export lands later).

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

Covers (37 tests): envelope/ULID shape, anonymous + token handshakes
(`?st=` carrier verified on the upgrade path), case create (with and without
the AOI-first `args.bbox`), a full chat round trip (streamed chunks, pipeline
steps, layer-event parse incl. a >64 KiB inline-GeoJSON frame exercising the
64-bit length path), cancel, s3->MinIO translation, the QGIS XYZ uri builder,
gate-card decision rules + proceed/cancel/narrow_scope round trips against a
gated stub turn, the hard-cap path, the mirrored cells/ETA/frames estimate
math, canvas-AOI CRS math + the 2-deg guard + status/attach formatting,
the capped-jitter backoff ladder, a reconnect that re-binds the case and
flushes the bounded outbound queue, sticky-anonymous replay, case-list
parsing, and the export-API client + exported-layer planning. GUI code is
intentionally untested here (no QGIS in the test env).

## Milestone 3 (next)

- Remote-mode presigned vector fetch + remote Open-in-QGIS (download the
  export artifacts through `/api/export-qgis/file`).
- Selected-polygon AOI (lasso) as an alternative to the canvas extent.
- Token acquisition (Cognito sign-in) instead of paste-only.
- Case switching from the Cases dialog (select an existing case for the chat
  session, not just export it).
