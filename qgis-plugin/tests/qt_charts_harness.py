"""Offscreen harness for the Charts panel (OpenQuake result parity,
live-feedback 2026-07-13).

Run as a SUBPROCESS by ``test_charts.TestChartsPanel`` -- it needs
``qgis.PyQt`` (PyQt5) + matplotlib, which the pure-python test venv does not
have; the wrapper probes the system interpreter and skips honestly when
absent (the ``qt_dock_ui_harness.py`` convention).

Offscreen, no agent, no network. Checks:

  1. HAZARD CURVE (the acceptance fixture's shape -- 19 IML points, layered
     line+rule spec, log-log scales, dashed 10%-in-50yr design line):
     ``set_charts`` renders it -- 2 views, 1 line series, 19 vertices,
     1 rule, x_log + y_log, the rule's label in the legend, axis titles.
  2. DE-DUPE: ``add_chart`` with the SAME chart_id returns False and does
     not grow the panel (a tool re-emit repaints, never duplicates).
  3. PAGING: a second chart (damage-distribution bar shape with a color
     field) pages to 2/2; prev steps back; the bar count is asserted.
  4. CLEAR: ``clear()`` hides the panel (case-switch discipline).
  5. DEFENSIVE: junk rows (no chart_id / non-dict spec) are skipped by
     ``set_charts``; a junk live payload returns False.
  6. DOCK WIRING: ``Trid3ntDock._on_event("chart", payload)`` lands the
     chart in the pinned panel and adds exactly ONE pointer note to the
     chat -- charts never flood the message list (NATE's clutter rule).

Exits 0 and prints CHARTS-OK plus the render summaries; asserts (nonzero)
otherwise. Also grabs docs/proof/97-qgis-charts-panel.png (offscreen
QWidget grab -- a LAYOUT proof, not pixel-parity vs live QGIS rendering).
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QLabel  # noqa: E402

# Never touch the real QGIS profile's QSettings from this harness.
QCoreApplication.setOrganizationName("trid3nt-charts-harness")
QCoreApplication.setApplicationName("trid3nt-charts-harness")

app = QApplication([])

from trid3nt import charts  # noqa: E402
from trid3nt.ui.dock import Trid3ntDock  # noqa: E402

PROOF_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)


def pump(n: int = 10) -> None:
    for _ in range(n):
        QCoreApplication.processEvents()


assert charts.matplotlib_available(), (
    "matplotlib must be importable in the QGIS python for this harness "
    "(the guarded text fallback exists for live, but the harness asserts "
    f"the real renderer): {charts._MATPLOTLIB_ERROR}"
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

# generate_damage_distribution shape: single-view bar + color field.
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

panel = charts.ChartsPanel()
panel.resize(420, 360)
panel.show()
pump()
assert panel.count == 0

n = panel.set_charts([HAZARD_CHART])
pump()
assert n == 1 and panel.count == 1, f"set_charts -> {n}, count={panel.count}"
assert panel.toggle.text() == "Charts (1)", panel.toggle.text()
assert panel.isVisible(), "panel hidden with a chart loaded"
s = panel.last_render_summary
print("hazard summary:", s)
assert s["views"] == 2, s
assert s["lines"] == 1 and s["series"] == 1, s
assert s["points"] == 19, s
assert s["rules"] == 1, s
assert s["x_log"] and s["y_log"], s
assert "10% in 50yr" in s["legend_labels"], s
assert panel.caption_label.isVisibleTo(panel), "caption not shown"
assert "474 sites" in panel.caption_label.text()

# Axis titles made it onto the axes.
fig_axes = panel._card.figure.axes  # noqa: SLF001 -- harness introspection
assert fig_axes[0].get_xlabel() == "PGA (g)", fig_axes[0].get_xlabel()
assert fig_axes[0].get_ylabel() == "Mean PoE in 50yr", fig_axes[0].get_ylabel()

# --------------------------------------------------------------------------- #
# 2. De-dupe on chart_id
# --------------------------------------------------------------------------- #

assert panel.add_chart(dict(HAZARD_CHART)) is False, "re-emit must not duplicate"
assert panel.count == 1, panel.count

# --------------------------------------------------------------------------- #
# 3. Paging: a second chart (bar + color field), prev/next
# --------------------------------------------------------------------------- #

assert panel.add_chart(DAMAGE_CHART) is True
pump()
assert panel.count == 2
assert panel.pos_label.text() == "2/2", panel.pos_label.text()
assert panel.current_chart_id() == DAMAGE_CHART["chart_id"]
s = panel.last_render_summary
print("damage summary:", s)
assert s["bars"] == 4, s
assert not s["x_log"] and not s["y_log"], s

panel.prev_btn.click()
pump()
assert panel.current_chart_id() == HAZARD_CHART["chart_id"]
assert panel.pos_label.text() == "1/2", panel.pos_label.text()

# Proof grab while both charts are loaded (hazard curve showing).
os.makedirs(PROOF_DIR, exist_ok=True)
panel.grab().save(os.path.join(PROOF_DIR, "97-qgis-charts-panel.png"))

# --------------------------------------------------------------------------- #
# 4. Clear hides (case-switch discipline)
# --------------------------------------------------------------------------- #

panel.clear()
pump()
assert panel.count == 0
assert not panel.isVisible(), "clear must hide the panel"

# --------------------------------------------------------------------------- #
# 5. Defensive parsing
# --------------------------------------------------------------------------- #

n = panel.set_charts([
    "junk", {"chart_id": "", "vega_lite_spec": {"mark": "line"}},
    {"chart_id": "01OK", "vega_lite_spec": "not-a-dict"},
    UHS_CHART,
])
assert n == 1 and panel.current_chart_id() == UHS_CHART["chart_id"], n
s = panel.last_render_summary
print("uhs summary:", s)
assert s["lines"] == 1 and s["points"] == 4 and not s["x_log"], s
assert panel.add_chart({"nope": True}) is False
panel.clear()

# --------------------------------------------------------------------------- #
# 6. Dock wiring: _on_event("chart") -> panel + ONE pointer note, no flood
# --------------------------------------------------------------------------- #


class FakeIface:
    """Headless iface (qt_dock_ui_harness convention): no canvas."""

    def mapCanvas(self):
        raise RuntimeError("headless harness has no canvas")

    def activeLayer(self):
        return None


dock = Trid3ntDock(FakeIface())
dock._auto_connect_done_this_show = True  # block showEvent auto-connect
dock.resize(420, 700)
dock.show()
pump()

before = dock.messages_layout.count()
dock._on_event("chart", HAZARD_CHART)
pump()
assert dock.charts_panel.count == 1
assert dock.charts_panel.current_chart_id() == HAZARD_CHART["chart_id"]
after = dock.messages_layout.count()
# Exactly one new chat widget: the pending assistant entry carrying the
# single pointer note -- never a chart widget in the message list.
assert after - before <= 1, (before, after)
notes = [
    lbl.text()
    for lbl in dock.messages_host.findChildren(QLabel)
    if "Chart added below" in lbl.text()
]
assert len(notes) == 1, notes
canvases = dock.messages_host.findChildren(type(dock.charts_panel._card))
assert not canvases, "a chart canvas leaked into the chat message list"

# A live RE-emit of the same chart adds no second note and no growth.
before = dock.messages_layout.count()
dock._on_event("chart", dict(HAZARD_CHART))
pump()
assert dock.charts_panel.count == 1
assert dock.messages_layout.count() == before

# Case-switch clear path (_clear_messages) empties the panel too.
dock._clear_messages()
pump()
assert dock.charts_panel.count == 0

print("CHARTS-OK")
