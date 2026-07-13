"""Live proof: OpenQuake result parity -- opening the kept PSHA case must
surface its persisted hazard-curve chart in the dock's Charts panel
(live-feedback 2026-07-13).

Loads the REAL plugin inside a real (offscreen) QgsApplication -- same
pattern as ``headless_case_switch_proof.py`` -- connects to the LIVE local
agent on ``ws://127.0.0.1:8765``, opens the kept acceptance case
"Probabilistic Seismic Hazard Analysis Run_seismi"
(01KXD9J5T0AW6FGNT1CKY0XD4G -- READ-ONLY select, the case is never mutated)
whose chart is persisted server-side (chart_id 01KXD9Q34VPR9C5AJ40DX8MN4M),
and asserts:

  1. the case-open replay populates the Charts panel: visible, count >= 1,
     the persisted chart_id is current;
  2. the render is the real hazard curve: 1 line series with 19 IML
     vertices, the dashed 10%-in-50yr design rule (legend label), log-log
     axes, PGA axis titles;
  3. the case's web-parity caption ("474 sites") rides along;
  4. NO chart widget landed in the chat message list (clutter rule);
  5. switching AWAY to a chart-less case clears/hides the panel.

Grabs the dock with the chart visible to docs/proof/98-qgis-oq-chart.png
(offscreen QWidget grab -- widget-layout proof, not pixel-parity vs live
QGIS; the map canvas is not part of the grab, the CHART is the subject).

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_oq_chart_proof.py

Set TRID3NT_AGENT_URL / TRID3NT_ANON_USER_ID / TRID3NT_OQ_CASE /
TRID3NT_OQ_CHART_ID to point at a different stack/case (defaults match the
kept fixture this proof was authored against).
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

AGENT_URL = os.environ.get("TRID3NT_AGENT_URL", "ws://127.0.0.1:8765/ws")
ANON_USER_ID = os.environ.get(
    "TRID3NT_ANON_USER_ID", "0110CA1VSERAAAAAAAAAAAAAAA"
)
OQ_CASE = os.environ.get("TRID3NT_OQ_CASE", "01KXD9J5T0AW6FGNT1CKY0XD4G")
OQ_CASE_TITLE = "Probabilistic Seismic Hazard Analysis Run_seismi"
OQ_CHART_ID = os.environ.get(
    "TRID3NT_OQ_CHART_ID", "01KXD9Q34VPR9C5AJ40DX8MN4M"
)
# Any OTHER case the same anon user owns, for the clear-on-switch check.
OTHER_CASE = os.environ.get("TRID3NT_OTHER_CASE", "01KX509BASVDVWJGMXC51P150J")
OTHER_CASE_TITLE = "Landcover Over Washington State"

PROOF_PNG = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "docs", "proof",
        "98-qgis-oq-chart.png",
    )
)

qgs = QgsApplication([], False)
qgs.initQgis()

failures: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        failures.append(label)


class FakeIface:
    """The minimal QgisInterface surface the plugin touches."""

    def __init__(self):
        self._win = QMainWindow()
        self._canvas = QgsMapCanvas()
        self._canvas.resize(900, 700)
        self._win.setCentralWidget(self._canvas)
        self._win.resize(1200, 800)

    def mainWindow(self):
        return self._win

    def mapCanvas(self):
        return self._canvas

    def activeLayer(self):
        return None

    def addDockWidget(self, area, dock):
        self._win.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self._win.removeDockWidget(dock)

    def addToolBarIcon(self, action):
        pass

    def removeToolBarIcon(self, action):
        pass

    def addPluginToMenu(self, *_a):
        pass

    def removePluginMenu(self, *_a):
        pass


iface = FakeIface()

from qgis.PyQt.QtCore import QSettings  # noqa: E402

from trid3nt.plugin import Trid3ntPlugin  # noqa: E402

# Stamp the agent URL + the KNOWN local anon user for this run, restored on
# exit (headless_case_switch_proof hygiene) -- the sticky anon id is what
# lets select_case against the kept case succeed (same local User record).
_qs = QSettings()
_prior = {
    k: _qs.value(k, None)
    for k in ("trid3nt/local_url", "trid3nt/anonymous_user_id", "trid3nt/canvas_aoi")
}
_qs.setValue("trid3nt/local_url", AGENT_URL)
_qs.setValue("trid3nt/anonymous_user_id", ANON_USER_ID)
_qs.setValue("trid3nt/canvas_aoi", "false")
_qs.sync()


def _restore() -> None:
    for k, v in _prior.items():
        if v is None:
            _qs.remove(k)
        else:
            _qs.setValue(k, v)
    _qs.sync()


def pump(seconds, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not until():
        QCoreApplication.processEvents()
        time.sleep(0.05)


plugin = Trid3ntPlugin(iface)
plugin.initGui()
plugin.toggle_dock(True)
dock = plugin.dock
print(f"[proof] dock: {dock.__class__.__name__ if dock else 'NOT FOUND'}", flush=True)
if dock is None:
    print("[proof] FAILED: no dock", flush=True)
    plugin.unload()
    _restore()
    qgs.exitQgis()
    sys.exit(2)

if not dock.bridge.running:
    dock.connect_agent()
pump(30, lambda: dock._case_id is not None)
print(f"[proof] connected={dock._connected} initial case={dock._case_id}", flush=True)
if dock._case_id is None:
    print("[proof] FAILED: no case bound (agent down?)", flush=True)
    plugin.unload()
    _restore()
    qgs.exitQgis()
    sys.exit(2)

# Settle past the startup-reuse's own case-open before driving our select
# (the headless_case_switch_proof timing note applies verbatim).
pump(15)
print(f"[proof] settled after connect: case={dock._case_id}", flush=True)

# --------------------------------------------------------------------------- #
# Open the kept PSHA case (READ-ONLY select) -- the chart must surface
# --------------------------------------------------------------------------- #

dock.select_case(OQ_CASE, OQ_CASE_TITLE)
pump(25, lambda: dock._case_id == OQ_CASE and dock.charts_panel.count > 0)
pump(3)  # trailing queued events (notes/zoom) settle

check("PSHA case bound", dock._case_id == OQ_CASE, str(dock._case_id))
check(
    "Charts panel populated on case-open replay",
    dock.charts_panel.count >= 1,
    f"count={dock.charts_panel.count}",
)
# isVisibleTo(dock), not isVisible(): the offscreen main window is never
# shown, so absolute visibility is False for every descendant regardless
# of the panel's own state -- isVisibleTo isolates the panel's flags.
check("Charts panel visible", dock.charts_panel.isVisibleTo(dock))
check(
    "persisted chart is current",
    dock.charts_panel.current_chart_id() == OQ_CHART_ID,
    str(dock.charts_panel.current_chart_id()),
)
check(
    "toggle reads Charts (N)",
    dock.charts_panel.toggle.text() == f"Charts ({dock.charts_panel.count})",
    dock.charts_panel.toggle.text(),
)

s = dock.charts_panel.last_render_summary or {}
print(f"[proof] render summary: {s}", flush=True)
check("hazard curve line series", s.get("lines") == 1 and s.get("series") == 1, str(s))
check("19 IML vertices", s.get("points") == 19, str(s.get("points")))
check("dashed design rule drawn", s.get("rules") == 1, str(s.get("rules")))
check("log-log axes", bool(s.get("x_log")) and bool(s.get("y_log")), str(s))
check(
    "10%-in-50yr legend label",
    "10% in 50yr" in (s.get("legend_labels") or []),
    str(s.get("legend_labels")),
)
axes = getattr(dock.charts_panel._card, "figure", None)
axes = axes.axes if axes is not None else []
check(
    "PGA axis titles",
    bool(axes) and axes[0].get_xlabel() == "PGA (g)"
    and "PoE" in axes[0].get_ylabel(),
    f"{axes[0].get_xlabel() if axes else '?'} / {axes[0].get_ylabel() if axes else '?'}",
)
check(
    "web-parity caption (474 sites)",
    "474 sites" in dock.charts_panel.caption_label.text(),
    dock.charts_panel.caption_label.text()[:80],
)

# Clutter rule: no matplotlib canvas inside the chat message list.
card_cls = type(dock.charts_panel._card)
in_chat = dock.messages_host.findChildren(card_cls)
check("no chart widget in chat message list", not in_chat, str(len(in_chat)))

# -- screenshot: the dock with the chart visible --------------------------- #
os.makedirs(os.path.dirname(PROOF_PNG), exist_ok=True)
dock.charts_panel.toggle.setChecked(True)
dock.charts_panel._toggle_body()
pump(1)
dock.resize(460, 900)
pump(1)
dock.grab().save(PROOF_PNG)
print(f"[proof] screenshot: {PROOF_PNG}", flush=True)
check("screenshot written", os.path.exists(PROOF_PNG))

# --------------------------------------------------------------------------- #
# Switch away to a chart-less case -- the panel must clear + hide
# --------------------------------------------------------------------------- #

dock.select_case(OTHER_CASE, OTHER_CASE_TITLE)
pump(20, lambda: dock._case_id == OTHER_CASE)
pump(3)
check("other case bound", dock._case_id == OTHER_CASE, str(dock._case_id))
check(
    "charts panel cleared on switch to chart-less case",
    dock.charts_panel.count == 0 and not dock.charts_panel.isVisibleTo(dock),
    f"count={dock.charts_panel.count} "
    f"visible={dock.charts_panel.isVisibleTo(dock)}",
)

# --------------------------------------------------------------------------- #

plugin.unload()
_restore()
qgs.exitQgis()

if failures:
    print(f"[proof] FAILED: {failures}", flush=True)
    sys.exit(1)
print("[proof] OQ-CHART-PROOF-OK", flush=True)
