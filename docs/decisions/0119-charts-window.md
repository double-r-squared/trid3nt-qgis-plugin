# ADR 0119 -- Charts move to a TUFLOW-Viewer-style bottom window

Status: accepted (2026-08-04, NATE charts-window directive)
Follows: 0106 (structured provenance -- the chart's `source_layer_uri` seam this
window links against). Supersedes the in-chat `ChartsPanel` surface introduced
live-feedback 2026-07-13 (OpenQuake result parity).

## Context

NATE's directive, verbatim: charts "should instead of being surfaced in chat get
there own window, the charts button should still be in chat and actually it
should show the window when clicked, the charts window should be something that
looks and operates similar to tuflow viewer, where it has interactive maps and
displays at the bottom of the app window horizontally."

Charts were rendered by `charts.ChartsPanel` -- a small collapsible "Charts (N)"
panel pinned UNDER the chat message list (the probe-panel pattern). It rendered
the agent's `chart-emission` Vega-Lite payloads on a matplotlib canvas but was
cramped (dock-column width), non-interactive (a static PNG-like canvas: no hover,
no pick, no zoom), and had no linkage to the QGIS map canvas. TUFLOW Viewer -- the
reference NATE named -- is a bottom-docked, wide, interactive plot surface with
hover readouts, click-to-inspect, and plot<->map linking.

## Decision

### 1. The window (replaces the in-chat panel)

A new `ui/charts_window.py::ChartsWindow(QDockWidget)`, docked to
`Qt.BottomDockWidgetArea` of the QGIS main window (the TUFLOW Viewer position).
Horizontal layout: a thin chart-list strip (left, a `QListWidget` of the
session's chart titles) for switching among charts; the matplotlib canvas
centre-stage; the matplotlib navigation toolbar above it; a status row below
(hover readout + "Locate on map"); caption + prev/next paging at the bottom.
Floating / re-docking is native `QDockWidget`.

The window is built LAZILY -- on the first chart (case-open replay or live
`chart-emission`) or the first "Charts (N)" button click -- so a chart-less
session never spawns a bottom dock. It is docked but NOT force-shown on case
open; the chat button invites it open. It survives a case switch (the bottom
dock stays put); only its chart LIST is cleared/rebuilt per case.

### 2. The chat button

The chat dock keeps a "Charts (N)" `QToolButton` (in the action row, beside
Probe / Set AOI). Click SHOWS + raises the window (creating it lazily). A live
chart increments the count and subtly flags the button ("Charts (N) *"); opening
the window clears the flag. Charts NEVER render inline in chat -- a live chart
lands in the window and drops ONE pointer note ("Chart added to the charts
window: <title>") into the transcript.

### 3. TUFLOW-esque interactivity (honestly scoped)

| # | affordance | status | mechanism |
| --- | --- | --- | --- |
| a | hover readout | BUILT | mpl `motion_notify_event` -> `x = .. y = ..` status row (data coords) |
| b | click-to-inspect | BUILT | mpl `button_press_event` -> `nearest_vertex` (display-space distance across every line series + scatter collection) highlights the vertex + shows its value + series label |
| c | x-zoom / pan | BUILT | embedded matplotlib `NavigationToolbar` (pan, rubber-band zoom, home) + a plain scroll-wheel that zooms the x-axis about the cursor |
| d | map linkage | BUILT (reachable half) | a chart carrying `source_layer_uri` gets a "Locate on map" button; click pans + flashes the QGIS canvas to that layer's extent (see 4) |

`nearest_vertex(x_pixel, y_pixel)` is pure geometry (no synthesized mouse event)
so the headless harness asserts the snap directly.

### 4. Map linkage + payload characterization

`ChartEmissionPayload` (contracts/chart_contracts.py) carries today:
`chart_id`, `vega_lite_spec` (opaque dict with the chart's `data.values`),
`title`, `caption`, **`source_layer_uri`** (optional `gs://`/`s3://` layer URI
the chart was computed from), `created_turn_id`. It carries NO per-feature
geometry: the vega `data.values` rows are the chart's plotted numbers (iml/poe,
period/sa, damage_state/count), not lon/lat per point, and there is no
station-id -> coordinate map.

So the reachable linkage is layer-level, not feature-level: "Locate on map"
matches `source_layer_uri` to a loaded QGIS layer and pans/flashes to its
extent. The materializer (`render/layers.py::_add_to_group`) now STAMPS every
materialized layer with `trid3nt/source_uri` (= the layer's render uri) +
`trid3nt/layer_id` custom properties; the dock's `_find_layer_by_source_uri`
matches by that stamp first, then by a substring match either direction against
the provider source (the chart's `gs://` uri and the render uri can differ in
scheme/host but share the object key). An unmatched uri is an HONEST note, never
a silent no-op.

**Server-side follow-up (NOT built here -- orchestrator to route):** full
TUFLOW-style feature-click -> plot linking needs the emission to carry
per-feature series data (a feature-id -> series map, or lon/lat on each
`data.values` row). That is a `chart_contracts.ChartEmissionPayload` addition +
a `generate_chart` / engine-postprocessor change (server tree, owned by another
seam) -- surfaced here as the named follow-up: **"chart-emission per-feature
geography"**. With it, the window could highlight the map feature a picked chart
vertex belongs to (and vice versa).

### 5. The renderer is unchanged (identity gate)

`charts.render_spec` + all `_draw_*` helpers + the pure payload/spec helpers move
NO logic -- they stay in `ui/charts.py`, which is now a Qt-widget-free pure
renderer module (its own `QWidget`/`QLabel`/etc. imports are dropped with the
panel). Verified byte-identical vs HEAD (the extracted `render_spec`.._draw_scatter
span compares equal). The window imports `Figure` / `FigureCanvasQTAgg` /
`render_spec` / `parse_chart_payload` from `charts`; the `NavigationToolbar` is
imported under the same guarded pattern.

## Deletion ledger (the shared `docs/DELETION_LEDGER.md` is owned by another
seam this wave -- recorded here for the orchestrator to transcribe)

| item | condition-to-delete | status |
| --- | --- | --- |
| `charts.ChartsPanel` (the in-chat collapsible "Charts (N)" panel, ~205 LOC: class + toggle/paging/card body) | superseded by `ChartsWindow` | DELETED (this wave) |
| `charts.py` Qt-widget imports (`Qt`, `QHBoxLayout`, `QLabel`, `QPushButton`, `QSizePolicy`, `QToolButton`, `QVBoxLayout`, `QWidget`) + `_CANVAS_HEIGHT` | dead once `ChartsPanel` is gone | DELETED (this wave) |
| `dock.py` `self.charts_panel` mount + `charts` module import | replaced by the lazy `ChartsWindow` + "Charts (N)" button | DELETED (this wave) |

## Consequences

- Charts get a wide, interactive, map-linked home; the chat stays clean (one
  pointer note per chart, never a canvas in the scroll).
- The per-case durability norm holds: the window's list rebuilds from the
  persisted `SessionChartRecord` replay on case open, clears on case switch.
- Headless-safe: no `iface.addDockWidget` (test FakeIface) leaves the window a
  standalone widget, still fully driveable for the list/persistence/interactivity
  logic. matplotlib absent -> honest text fallback, never a crash.
- Feature-level map<->plot linking is deferred behind the named server-side
  payload follow-up; the layer-level "Locate on map" ships now.
- The plugin version bump + repackage are the orchestrator's at close-out (this
  wave runs concurrently with the SCHISM server landing and touches only
  `qgis-plugin/**`).
