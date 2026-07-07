"""Headless first-run proof: load the plugin inside a real QgsApplication
(offscreen), connect to the LIVE local agent, request a DEM, wait for the
layer to land in QgsProject, and grab screenshots.

This is a MANUAL live-proof driver, not part of ``make test`` (it needs the
local TRID3NT stack up on ws://127.0.0.1:8765 and drives a real LLM turn,
which may queue behind other work). It exists because only a real Qt object
tree catches Qt-wiring crashes -- the QObject.event() signal-shadowing abort
that shipped in milestone 2 reproduced on the FIRST connect here.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_first_run.py

By default the CURRENT repo tree is loaded (what you are editing). Set
TRID3NT_PLUGIN_PATH to a directory containing a ``trid3nt`` package (e.g.
the installed profile plugins dir) to drive that copy instead.

Set TRID3NT_AGENT_URL to point the dock at a different agent (e.g. the test
stub server) -- the QSettings ``trid3nt/local_url`` key is stamped for the
run and RESTORED afterwards, so your real QGIS profile setting survives.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow, QPushButton  # noqa: E402

PROOF = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

qgs = QgsApplication([], False)
qgs.initQgis()


class FakeIface:
    """The minimal QgisInterface surface the plugin touches."""

    def __init__(self):
        self._win = QMainWindow()
        self._canvas = QgsMapCanvas()
        self._canvas.resize(900, 700)
        self._win.setCentralWidget(self._canvas)
        self._win.resize(1500, 900)

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

# Give the canvas a real CRS + a downtown-Tampa extent (EPSG:3857) so the
# dock's canvas-AOI path resolves and the agent receives a genuine bbox --
# without this the AOI line reads "CRS not resolved -- sent without AOI" and
# the model geocodes a ~10 m degenerate bbox.
from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle  # noqa: E402

iface.mapCanvas().setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
iface.mapCanvas().setExtent(
    QgsRectangle(-9184000, 3235000, -9178000, 3241000)  # ~6 km downtown Tampa
)

# OSM basemap so the proof screenshot shows a real map under the agent layer
# (loads straight from tile.openstreetmap.org -- no agent involvement).
from qgis.core import QgsProject as _QgsProject, QgsRasterLayer  # noqa: E402

_osm = QgsRasterLayer(
    "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
    "&zmax=19&zmin=0",
    "OpenStreetMap",
    "wms",
)
if _osm.isValid():
    _QgsProject.instance().addMapLayer(_osm)
    print("[first-run] OSM basemap added", flush=True)

from qgis.PyQt.QtCore import QSettings  # noqa: E402

from trid3nt.plugin import Trid3ntPlugin  # noqa: E402

# Optional agent-URL override (stub-server runs) -- stamped for THIS run,
# restored on exit so the user's real profile setting survives. The sticky
# anonymous_user_id is saved/restored too: a stub run stores the STUB's fake
# user id, and replaying that against the real agent poisons every later
# handshake (this exact leak shipped once -- the settings-side ULID guard is
# the backstop, this is the hygiene fix at the source).
_URL_OVERRIDE = os.environ.get("TRID3NT_AGENT_URL")
_PRIOR_URL = None
_PRIOR_ANON = None
if _URL_OVERRIDE:
    _qs = QSettings()
    _PRIOR_URL = _qs.value("trid3nt/local_url", None)
    _PRIOR_ANON = _qs.value("trid3nt/anonymous_user_id", None)
    _qs.setValue("trid3nt/local_url", _URL_OVERRIDE)
    print(f"[first-run] agent url override: {_URL_OVERRIDE}", flush=True)


def _restore_url() -> None:
    if not _URL_OVERRIDE:
        return
    _qs = QSettings()
    for key, prior in (
        ("trid3nt/local_url", _PRIOR_URL),
        ("trid3nt/anonymous_user_id", _PRIOR_ANON),
    ):
        if prior is None:
            _qs.remove(key)
        else:
            _qs.setValue(key, prior)
    _qs.sync()


print(f"[first-run] plugin path: {PLUGIN_PATH}", flush=True)
plugin = Trid3ntPlugin(iface)
plugin.initGui()
plugin.toggle_dock(True)
dock = plugin.dock
print(
    "[first-run] dock:",
    dock.__class__.__name__ if dock else "NOT FOUND",
    flush=True,
)

iface.mainWindow().show()


def pump(seconds, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not until():
        QCoreApplication.processEvents()
        time.sleep(0.05)


# connect (defaults = local mode) -- the milestone 2 crash aborted HERE.
dock.connect_agent()
print("[first-run] connect_agent() called", flush=True)

pump(30, lambda: dock._case_id is not None)
print(
    f"[first-run] connected={dock._connected} case={dock._case_id}",
    flush=True,
)
if dock._case_id is None:
    print("[first-run] FAILED: no case bound (agent down?)", flush=True)
    plugin.unload()
    _restore_url()
    qgs.exitQgis()
    sys.exit(2)

# send the prompt through the dock's input. fetch_dem opts OUT of the
# server's deterministic auto-publish (raw input rasters are not
# auto-rendered), so ask for the render explicitly; if the small model
# still stops after the fetch, nudge once with a follow-up -- the same
# two-step a real user types.
dock.input_edit.setText(
    "Fetch a digital elevation model for downtown Tampa, Florida, "
    "and render it on the map."
)
dock._send()
print("[first-run] prompt sent via input_edit + _send()", flush=True)

# wait up to 9 min for a layer to land, pumping events (confirm gates too)
t0 = time.time()
clicked_gates: set = set()
nudged = False
while time.time() - t0 < 540:
    if not nudged and time.time() - t0 > 240:
        nudged = True
        dock.input_edit.setText(
            "Call the publish_layer tool now, passing the layer handle "
            "that fetch_dem returned, to render the elevation layer."
        )
        dock._send()
        print("[first-run] no layer yet -- publish nudge sent", flush=True)
    pump(5)
    # auto-proceed any (enabled, unanswered) gate card
    for btn in iface.mainWindow().findChildren(QPushButton):
        if (
            btn.text() == "Proceed"
            and btn.isEnabled()
            and id(btn) not in clicked_gates
        ):
            clicked_gates.add(id(btn))
            btn.click()
            print("[first-run] clicked gate: Proceed", flush=True)
    # agent-delivered layers only -- the driver-added basemap doesn't count
    agent_layers = [
        lyr
        for lyr in QgsProject.instance().mapLayers().values()
        if lyr.name() != "OpenStreetMap"
    ]
    if agent_layers:
        print(
            f"[first-run] {len(agent_layers)} agent layer(s) in project "
            f"after {time.time() - t0:.0f}s",
            flush=True,
        )
        break

# zoom canvas to the agent layer over the basemap + render
layers = [
    lyr
    for lyr in QgsProject.instance().mapLayers().values()
    if lyr.name() != "OpenStreetMap"
]
if layers:
    all_layers = layers + [
        lyr
        for lyr in QgsProject.instance().mapLayers().values()
        if lyr.name() == "OpenStreetMap"
    ]
    iface.mapCanvas().setLayers(all_layers)
    iface.mapCanvas().setExtent(layers[0].extent())
    iface.mapCanvas().refresh()
    pump(12)

os.makedirs(PROOF, exist_ok=True)
# A failed run must never clobber a prior successful proof -- failure shots
# go to the *-failed names for diagnosis.
_suffix = "" if layers else "-failed"
iface.mainWindow().grab().save(
    os.path.join(PROOF, f"40-qgis-plugin-firstrun{_suffix}.png")
)
dock.grab().save(os.path.join(PROOF, f"41-qgis-plugin-dock{_suffix}.png"))
print(
    "[first-run] screenshots saved; layers:",
    [layer.name() for layer in layers],
    flush=True,
)

plugin.unload()
_restore_url()
qgs.exitQgis()
print(
    "[first-run] DONE ok" if layers else "[first-run] DONE but NO LAYERS",
    flush=True,
)
sys.exit(0 if layers else 1)
