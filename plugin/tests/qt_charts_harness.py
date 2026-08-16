"""Offscreen harness for the ChartsWindow (charts-window 2026-08-04, NATE's
TUFLOW-Viewer directive).

Run as a SUBPROCESS by ``test_charts.TestChartsWindow`` -- it needs
``qgis.PyQt`` (PyQt5) + matplotlib, which the pure-python test venv does not
have; the wrapper probes the system interpreter and skips honestly when
absent (the ``qt_dock_ui_harness.py`` convention).

Offscreen, no agent, no network. Checks:

  1. HAZARD CURVE (the acceptance fixture's shape -- 19 IML points, layered
     line+rule spec, log-log scales, dashed 10%-in-50yr design line):
     ``set_charts`` renders it -- 2 views, 1 line series, 19 vertices,
     1 rule, x_log + y_log, the rule's label in the legend, axis titles.
  2. DE-DUPE: ``add_chart`` with the SAME chart_id returns False and does
     not grow the window (a tool re-emit repaints, never duplicates).
  3. PAGING + LIST STRIP: a second chart (damage-distribution bar shape with
     a color field) pages to 2/2; the chart-list strip carries both titles;
     prev steps back; the bar count is asserted.
  4. CLEAR: ``clear()`` empties the window (case-switch discipline).
  5. DEFENSIVE: junk rows (no chart_id / non-dict spec) are skipped by
     ``set_charts``; a junk live payload returns False.
  6. INTERACTIVITY: (b) ``nearest_vertex`` snaps a display-space point to the
     exact plotted vertex + carries its series label; (d) the "Locate on map"
     button enables only for a chart carrying a ``source_layer_uri`` and its
     click fires the locate callback with that uri.
  7. DOCK WIRING: ``Trid3ntDock._on_event("chart", payload)`` lazily builds
     the bottom window, lands the chart there, bumps the chat "Charts (N)"
     button (with the new-chart flag), and adds exactly ONE pointer note to
     the chat -- charts never flood the message list (NATE's clutter rule).

Exits 0 and prints CHARTS-OK plus the render summaries; asserts (nonzero)
otherwise. Also grabs docs/proof/97-qgis-charts-window.png (offscreen QWidget
grab -- a LAYOUT proof, not pixel-parity vs live QGIS rendering).
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QLabel  # noqa: E402

# Never touch the real QGIS profile's QSettings from this harness.
QCoreApplication.setOrganizationName("trid3nt-charts-harness")
QCoreApplication.setApplicationName("trid3nt-charts-harness")

app = QApplication([])

from plugin.ui import charts  # noqa: E402
from plugin.ui.charts_window import ChartsWindow  # noqa: E402
from plugin.ui.dock import Trid3ntDock  # noqa: E402

PROOF_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)


def pump(n: int = 10) -> None:
    for _ in range(n):
        QCoreApplication.processEvents()


assert charts.matplotlib_available(), (
    "matplotlib must be importable in the QGIS python for this harness "
    "(the guarded text fallback exists for live, but the harness asserts "
    f"the real renderer): {charts.matplotlib_error()}"
)

# --------------------------------------------------------------------------- #
# Fixtures -- the exact spec shapes chart_tools.py emits
# --------------------------------------------------------------------------- #

# build_hazard_curve_chart shape: 19 positive IML points (the acceptance
# case 01KXD9J5T0AW6FGNT1CKY0XD4G persists exactly this), layered line+rule,
# log-log, dashed design-level rule.
_IMLS = [
    0.005, 0.007, 0.0098, 0.0137, 0.0192, 0.0269, 0.0376, 0.0527, 0.0738,
    0.103, 0.145, 0.203, 0.284, 0.397, 0.556, 0.778, 1.09, 1.52, 2.13,
]
_POES = [
    1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9999995, 0.999849, 0.994751,
    0.954447, 0.835100, 0.620279, 0.366257, 0.167281, 0.058937, 0.015494,
    0.002923, 0.000489,
]
HAZARD_CHART = {
    "envelope_type": "chart-emission",
    "chart_id": "01HARNESSHAZARDAAAAAAAAAAA",
    "title": "Seismic hazard curve - PGA",
    "caption": "Mean PGA hazard curve over 50yr; dashed line = 10% in 50yr "
               "design level - 19 IML points - 474 sites",
    "source_layer_uri": "s3://trid3nt-data/cases/psha/hazard_sites.geojson",
    "vega_lite_spec": {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "Seismic hazard curve - PGA",
        "width": "container",
        "layer": [
            {
                "data": {"values": [
                    {"iml": x, "poe": p} for x, p in zip(_IMLS, _POES)
                ]},
                "mark": {"type": "line", "point": True, "tooltip": True},
                "encoding": {
                    "x": {"field": "iml", "type": "quantitative",
                          "scale": {"type": "log"}, "title": "PGA (g)"},
                    "y": {"field": "poe", "type": "quantitative",
                          "scale": {"type": "log"},
                          "title": "Mean PoE in 50yr"},
                },
            },
            {
                "data": {"values": [
                    {"poe_level": 0.1, "label": "10% in 50yr"}
                ]},
                "mark": {"type": "rule", "strokeDash": [4, 4],
                         "color": "#c1121f"},
                "encoding": {"y": {"field": "poe_level",
                                   "type": "quantitative"}},
            },
        ],
    },
}

# damage-state shape: single-view bar + color field (generate_chart). No
# source_layer_uri -> its Locate-on-map button stays disabled.
DAMAGE_CHART = {
    "envelope_type": "chart-emission",
    "chart_id": "01HARNESSDAMAGEAAAAAAAAAAA",
    "title": "Damage distribution",
    "caption": "Structures per damage state",
    "vega_lite_spec": {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "Damage distribution",
        "data": {"values": [
            {"damage_state": "none", "count": 120, "ds_index": 0},
            {"damage_state": "slight", "count": 45, "ds_index": 1},
            {"damage_state": "moderate", "count": 22, "ds_index": 2},
            {"damage_state": "complete", "count": 7, "ds_index": 3},
        ]},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {"field": "damage_state", "type": "nominal"},
            "y": {"field": "count", "type": "quantitative",
                  "title": "structures"},
            "color": {"field": "ds_index", "type": "nominal"},
        },
    },
}

# build_uhs_chart shape: single-view line, LINEAR axes (no log scale).
UHS_CHART = {
    "envelope_type": "chart-emission",
    "chart_id": "01HARNESSUHSAAAAAAAAAAAAAA",
    "title": "Uniform hazard spectrum",
    "caption": "Mean SA vs period",
    "vega_lite_spec": {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "Uniform hazard spectrum",
        "data": {"values": [
            {"period": 0.0, "sa": 0.42}, {"period": 0.2, "sa": 0.95},
            {"period": 0.5, "sa": 0.61}, {"period": 1.0, "sa": 0.33},
        ]},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "period", "type": "quantitative",
                  "title": "Spectral period (s)"},
            "y": {"field": "sa", "type": "quantitative",
                  "title": "Mean SA (g)"},
        },
    },
}

# --------------------------------------------------------------------------- #
# 1. Hazard curve renders with the full log-log + rule chrome
# --------------------------------------------------------------------------- #

_located = []
window = ChartsWindow(locate_callback=lambda uri: _located.append(uri))
window.resize(760, 300)
window.show()
pump()
assert window.count == 0

n = window.set_charts([HAZARD_CHART])
pump()
assert n == 1 and window.count == 1, f"set_charts -> {n}, count={window.count}"
assert window.isVisible(), "window hidden with a chart loaded"
s = window.last_render_summary
print("hazard summary:", s)
assert s["views"] == 2, s
assert s["lines"] == 1 and s["series"] == 1, s
assert s["points"] == 19, s
assert s["rules"] == 1, s
assert s["x_log"] and s["y_log"], s
assert "10% in 50yr" in s["legend_labels"], s
assert window.caption_label.isVisibleTo(window), "caption not shown"
assert "474 sites" in window.caption_label.text()
# The chart-list strip carries the title.
assert window.chart_list.count() == 1
assert window.chart_list.item(0).text() == "Seismic hazard curve - PGA"

# Axis titles made it onto the axes.
fig_axes = window._figure.axes  # noqa: SLF001 -- harness introspection
assert fig_axes[0].get_xlabel() == "PGA (g)", fig_axes[0].get_xlabel()
assert fig_axes[0].get_ylabel() == "Mean PoE in 50yr", fig_axes[0].get_ylabel()

# --------------------------------------------------------------------------- #
# 6a. Click-to-inspect (b): nearest_vertex snaps to the exact plotted vertex.
# --------------------------------------------------------------------------- #

ax = window._ax  # noqa: SLF001
target = (_IMLS[9], _POES[9])  # a real vertex (iml=0.103, poe=0.994751)
px, py = ax.transData.transform(target)
hit = window.nearest_vertex(px, py)
print("nearest_vertex:", hit)
assert hit is not None, "nearest_vertex found nothing"
assert abs(hit[0] - target[0]) < 1e-6 and abs(hit[1] - target[1]) < 1e-6, hit

# --------------------------------------------------------------------------- #
# 6b. Locate-on-map (d): enabled for a source-bearing chart; click fires the
#     callback with that uri.
# --------------------------------------------------------------------------- #

assert window.locate_btn.isEnabled(), "Locate-on-map disabled for a source chart"
window.locate_btn.click()
pump()
assert _located == [HAZARD_CHART["source_layer_uri"]], _located

# --------------------------------------------------------------------------- #
# 2. De-dupe on chart_id
# --------------------------------------------------------------------------- #

assert window.add_chart(dict(HAZARD_CHART)) is False, "re-emit must not duplicate"
assert window.count == 1, window.count

# --------------------------------------------------------------------------- #
# 3. Paging + list strip: a second chart (bar + color field), prev/next
# --------------------------------------------------------------------------- #

assert window.add_chart(DAMAGE_CHART) is True
pump()
assert window.count == 2
assert window.pos_label.text() == "2/2", window.pos_label.text()
assert window.current_chart_id() == DAMAGE_CHART["chart_id"]
assert window.chart_list.count() == 2
assert window.chart_list.currentRow() == 1
s = window.last_render_summary
print("damage summary:", s)
assert s["bars"] == 4, s
assert not s["x_log"] and not s["y_log"], s
# The damage chart has no source layer -> Locate-on-map disabled.
assert not window.locate_btn.isEnabled(), "Locate must disable without a source"

# Proof grab while both charts are loaded (damage bar showing).
os.makedirs(PROOF_DIR, exist_ok=True)
window.grab().save(os.path.join(PROOF_DIR, "97-qgis-charts-window.png"))

window.prev_btn.click()
pump()
assert window.current_chart_id() == HAZARD_CHART["chart_id"]
assert window.pos_label.text() == "1/2", window.pos_label.text()
assert window.chart_list.currentRow() == 0

# The list strip drives selection too.
window.chart_list.setCurrentRow(1)
pump()
assert window.current_chart_id() == DAMAGE_CHART["chart_id"], window.current_chart_id()

# --------------------------------------------------------------------------- #
# 4. Clear empties (case-switch discipline)
# --------------------------------------------------------------------------- #

window.clear()
pump()
assert window.count == 0
assert window.chart_list.count() == 0

# --------------------------------------------------------------------------- #
# 5. Defensive parsing
# --------------------------------------------------------------------------- #

n = window.set_charts([
    "junk", {"chart_id": "", "vega_lite_spec": {"mark": "line"}},
    {"chart_id": "01OK", "vega_lite_spec": "not-a-dict"},
    UHS_CHART,
])
assert n == 1 and window.current_chart_id() == UHS_CHART["chart_id"], n
s = window.last_render_summary
print("uhs summary:", s)
assert s["lines"] == 1 and s["points"] == 4 and not s["x_log"], s
assert window.add_chart({"nope": True}) is False
window.clear()

# --------------------------------------------------------------------------- #
# 7. Dock wiring: _on_event("chart") -> lazy window + button + ONE pointer note
# --------------------------------------------------------------------------- #


class FakeIface:
    """Headless iface (qt_dock_ui_harness convention): no canvas, no main
    window -- ``addDockWidget`` raises so the window stays a standalone
    widget, still fully driveable for the logic assertions."""

    def mapCanvas(self):
        raise RuntimeError("headless harness has no canvas")

    def activeLayer(self):
        return None


dock = Trid3ntDock(FakeIface())
dock._auto_connect_done_this_show = True  # block showEvent auto-connect
dock.resize(420, 700)
dock.show()
pump()

# No window and no charts until the first chart arrives.
assert dock._charts_window is None
assert dock.charts_btn.text() == "Charts (0)"

before = dock.messages_layout.count()
dock._on_event("chart", HAZARD_CHART)
pump()
assert dock._charts_window is not None, "chart did not lazily build the window"
assert dock._charts_window.count == 1
assert dock._charts_window.current_chart_id() == HAZARD_CHART["chart_id"]
# The chat button now shows the count + the new-chart flag.
assert dock.charts_btn.text() == "Charts (1) *", dock.charts_btn.text()
after = dock.messages_layout.count()
# Exactly one new chat widget: the pending assistant entry carrying the
# single pointer note -- never a chart widget in the message list.
assert after - before <= 1, (before, after)
notes = [
    lbl.text()
    for lbl in dock.messages_host.findChildren(QLabel)
    if "charts window" in lbl.text()
]
assert len(notes) == 1, notes
# No matplotlib canvas leaked into the chat message list.
canvas_type = type(dock._charts_window._canvas)
canvases = dock.messages_host.findChildren(canvas_type)
assert not canvases, "a chart canvas leaked into the chat message list"

# The "Charts (N)" button raises the window + clears the flag.
dock._show_charts_window()
pump()
assert dock._charts_window.isVisible()
assert dock.charts_btn.text() == "Charts (1)", dock.charts_btn.text()

# A live RE-emit of the same chart adds no second note and no growth.
before = dock.messages_layout.count()
dock._on_event("chart", dict(HAZARD_CHART))
pump()
assert dock._charts_window.count == 1
assert dock.messages_layout.count() == before

# Case-switch clear path (_clear_messages) empties the window + resets button.
dock._clear_messages()
pump()
assert dock._charts_window.count == 0
assert dock.charts_btn.text() == "Charts (0)", dock.charts_btn.text()

print("CHARTS-OK")
