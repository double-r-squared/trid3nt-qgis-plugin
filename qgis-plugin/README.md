# TRID3NT QGIS Plugin

The TRID3NT agent, docked inside QGIS: ask for data or a hazard simulation in
plain language, and the answer arrives as styled layers on the canvas you are
already working in.

This is **milestone 1 of v1** (per
`docs/design/qgis-plugin-product-analysis-2026-07.md`): plugin skeleton +
connection layer + chat dock + layer materialization. The sim granularity-gate
card and canvas-extent AOI injection land in milestone 2.

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

Covers: envelope/ULID shape, anonymous + token handshakes (`?st=` carrier
verified on the upgrade path), case create, a full chat round trip (streamed
chunks, pipeline steps, layer-event parse incl. a >64 KiB inline-GeoJSON frame
exercising the 64-bit length path), cancel, s3->MinIO translation, and the
QGIS XYZ uri builder. GUI code is intentionally untested here (no QGIS in the
test env).

## Milestone 2 (next)

- Sim gate card: render `tool-payload-warning` as a real Qt card with
  proceed / cancel / revise (the resolution "granularity gate" -- a user
  lever, always shown before a solve starts). The client verb
  (`confirm_payload`) already exists.
- Canvas-extent AOI: inject the current QGIS canvas extent (or selected
  polygon) as AOI context on every `user-message` turn.
- Reconnect policy (auto-retry with the stored anonymous user id), case
  reopen ("Open case..." over the existing exporter), remote-mode presigned
  vector fetch.
