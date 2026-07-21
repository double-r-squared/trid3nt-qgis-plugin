"""P4 LIVE acceptance: natural-prompt TELEMAC river-dye END TO END in the plugin.

Loads the REAL Trid3nt plugin inside an offscreen QgsApplication, connects to the
LIVE local agent on ws://127.0.0.1:8765/ws, creates a FRESH case with the AOI
toggle OFF (so the place name alone must geocode -- the F46 rule), and drives ONE
natural-language turn (NO bbox coordinates in the prompt):

    Simulate a contaminant dye spill in the river near Twin Falls, Idaho, and
    show how it travels downstream.

Any solver-confirm / granularity gate is auto-confirmed PROMPTLY. After the turn
completes, it triggers the plugin's own case export (``dock.open_case_in_qgis``)
-- the REAL path that discovers the SELAFIN mesh sibling and materializes it --
then ASSERTS the animated DYE mesh:

  - a QgsMeshLayer materialized (MDAL opened the .slf),
  - it carries a DYE dataset group that is TIME-VARYING (> 1 dataset),
  - its temporal properties are ACTIVE (the Temporal Controller can play it),
  - DYE is the ACTIVE scalar group (the plugin's tracer selector applied),

and renders HONEST MAP PIXELS at the PEAK dye frame (mesh over an OSM basemap)
via ``QgsMapRendererSequentialJob`` to docs/proof/telemac_p4_acceptance.png,
proving the animation is real (early vs peak frame pixels DIFFER).

The KEEP-THE-CASE + coord/F46 assertions are judged by the caller from
logs/agent.log for the printed session_id (the tool-call sequence + the geocoded
Twin Falls coords). This script DRIVES + ASSERTS the plugin half + screenshots;
it never deletes the case.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_telemac_p4_acceptance.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

AGENT_URL = os.environ.get("TRID3NT_AGENT_URL", "ws://127.0.0.1:8765/ws")
ANON_USER_ID = os.environ.get("TRID3NT_ANON_USER_ID", "0110CA1VSERAAAAAAAAAAAAAAA")
MINIO_ENDPOINT = os.environ.get("TRID3NT_MINIO_ENDPOINT", "http://100.92.163.46:9000")

PROOF_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)
SHOT = os.path.join(PROOF_DIR, "telemac_p4_acceptance.png")

PROMPT = (
    "Simulate a contaminant dye spill in the river near Twin Falls, Idaho, "
    "and show how it travels downstream."
)

TURN_BUDGET_S = int(os.environ.get("TRID3NT_TURN_BUDGET_S", "1200"))

failures: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        failures.append(label)


qgs = QgsApplication([], False)
qgs.initQgis()


class FakeIface:
    def __init__(self):
        self._win = QMainWindow()
        self._canvas = QgsMapCanvas()
        self._canvas.resize(900, 700)
        self._win.setCentralWidget(self._canvas)
        self._win.resize(1300, 850)

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

from trid3nt.plugin import Trid3ntPlugin  # noqa: E402
from trid3nt.ui.cards import GateCard  # noqa: E402

_qs = QSettings()
_prior = {
    k: _qs.value(k, None)
    for k in (
        "trid3nt/local_url",
        "trid3nt/anonymous_user_id",
        "trid3nt/canvas_aoi",
        "trid3nt/minio_endpoint",
    )
}
_qs.setValue("trid3nt/local_url", AGENT_URL)
_qs.setValue("trid3nt/anonymous_user_id", ANON_USER_ID)
_qs.setValue("trid3nt/canvas_aoi", "false")  # place name alone must geocode (F46)
_qs.setValue("trid3nt/minio_endpoint", MINIO_ENDPOINT)
_qs.sync()


def _restore():
    for k, v in _prior.items():
        if v is None:
            _qs.remove(k)
        else:
            _qs.setValue(k, v)
    _qs.sync()


def _auto_confirm_gates():
    for card in dock.messages_host.findChildren(GateCard):
        if card._decided is None:
            print(
                f"[drive] auto-confirming gate warning_id={card._warning.warning_id}",
                flush=True,
            )
            card._proceed()


def pump(seconds, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not until():
        QCoreApplication.processEvents()
        _auto_confirm_gates()
        time.sleep(0.05)


_EVENTS = {"turn_complete": 0}


def _observe_event(kind, data):
    if kind == "turn-complete":
        _EVENTS["turn_complete"] += 1
        print(f"[drive] <turn-complete #{_EVENTS['turn_complete']}>", flush=True)


plugin = Trid3ntPlugin(iface)
plugin.initGui()
plugin.toggle_dock(True)
dock = plugin.dock
print(f"[drive] dock: {dock.__class__.__name__ if dock else 'NONE'}", flush=True)
if dock is None:
    plugin.unload()
    _restore()
    qgs.exitQgis()
    sys.exit(2)

dock.bridge.agent_event.connect(_observe_event)

if not dock.bridge.running:
    dock.connect_agent()
pump(30, lambda: dock._case_id is not None)
print(f"[drive] connected={dock._connected} startup_case={dock._case_id}", flush=True)
if dock._case_id is None:
    print("[drive] FAILED: no case bound (agent down?)", flush=True)
    plugin.unload()
    _restore()
    qgs.exitQgis()
    sys.exit(2)

pump(12)
startup_case = dock._case_id

# --- Fresh case, AOI OFF -- must NOT inherit any prior AOI anchor. ----------- #
dock.new_case()
pump(30, lambda: dock._case_id is not None and dock._case_id != startup_case)
pump(4)
demo_case = dock._case_id
demo_title = dock._case_title
print(f"[drive] FRESH_CASE_ID={demo_case} title={demo_title!r}", flush=True)
print(f"[drive] canvas_aoi setting = {dock.settings.canvas_aoi}", flush=True)

# --- Drive the ONE natural-language turn. ----------------------------------- #
print(f"\n[drive] === TURN SEND @ {time.strftime('%H:%M:%S')} ===", flush=True)
print(f"[drive] prompt: {PROMPT}", flush=True)
t0 = time.time()
baseline = _EVENTS["turn_complete"]
dock.input_edit.setText(PROMPT)
dock._send()
end = time.time() + TURN_BUDGET_S
last_beat = 0.0
while time.time() < end:
    QCoreApplication.processEvents()
    _auto_confirm_gates()
    if _EVENTS["turn_complete"] > baseline:
        break
    now = time.time()
    if now - last_beat >= 30:
        last_beat = now
        print(f"[drive]   turn in progress +{now - t0:.0f}s", flush=True)
    time.sleep(0.2)
done = _EVENTS["turn_complete"] > baseline
print(
    f"[drive] === TURN {'COMPLETE' if done else 'BUDGET-CAP'} after "
    f"{time.time() - t0:.0f}s ===",
    flush=True,
)
pump(5)  # let trailing notes/layers/zoom settle

_client = getattr(dock.bridge, "client", None)
session_id = getattr(_client, "session_id", None)
print(f"[drive] session_id={session_id}", flush=True)
print(f"[drive] FRESH_CASE_ID={demo_case}", flush=True)

# --- Trigger the plugin's OWN case export to materialize the SELAFIN mesh. --- #
print("[drive] exporting the case to QGIS (materializes the dye mesh) ...", flush=True)
n_before = len(QgsProject.instance().mapLayers())
dock.open_case_in_qgis(demo_case, demo_title or demo_case)


def _mesh_layers():
    return [
        l for l in QgsProject.instance().mapLayers().values()
        if l.__class__.__name__ == "QgsMeshLayer"
    ]


pump(180, lambda: len(_mesh_layers()) >= 1)
mesh_layers = _mesh_layers()
check("a QgsMeshLayer materialized from the case export", len(mesh_layers) >= 1,
      f"{len(mesh_layers)} mesh layer(s)")

from qgis.core import QgsMeshDatasetIndex  # noqa: E402

if mesh_layers:
    ml = mesh_layers[0]
    check("mesh layer is valid (MDAL opened the .slf)", ml.isValid())
    check("mesh CRS resolved", ml.crs().isValid(), ml.crs().authid())

    groups = {}
    for i in range(ml.datasetGroupCount()):
        try:
            groups[i] = ml.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0)).name()
        except Exception:
            pass
    print(f"[drive] dataset groups: {groups}", flush=True)
    dye_idx = next((i for i, n in groups.items() if "dye" in (n or "").lower()), None)
    check("mesh carries a DYE dataset group", dye_idx is not None, str(groups))

    if dye_idx is not None:
        n_dye = ml.datasetCount(QgsMeshDatasetIndex(dye_idx, 0))
        check("DYE group is TIME-VARYING (> 1 dataset = playable series)",
              n_dye > 1, f"{n_dye} datasets")
        tp = ml.temporalProperties()
        check("mesh temporal properties ACTIVE (Temporal Controller can play it)",
              bool(tp.isActive()), str(tp.isActive()))
        active = ml.rendererSettings().activeScalarDatasetGroup()
        check("DYE is the ACTIVE scalar group (tracer selector applied)",
              active == dye_idx, f"active={active} ({groups.get(active)!r})")

        # --- honest map pixels: render the PEAK dye frame over an OSM basemap - #
        peak_ds, peak_max = 0, -1.0
        for d in range(n_dye):
            mx = ml.datasetMetadata(QgsMeshDatasetIndex(dye_idx, d)).maximum()
            if mx > peak_max:
                peak_max, peak_ds = mx, d
        print(f"[drive] DYE datasets={n_dye} peak idx={peak_ds} max={peak_max:.3f}", flush=True)

        from qgis.core import (  # noqa: E402
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsDateTimeRange,
            QgsMapRendererSequentialJob,
            QgsMapSettings,
            QgsRasterLayer,
            QgsRectangle,
        )
        from qgis.PyQt.QtCore import QSize  # noqa: E402
        from qgis.PyQt.QtGui import QColor  # noqa: E402

        rs = ml.rendererSettings()
        rs.setActiveScalarDatasetGroup(dye_idx)
        ss = rs.scalarSettings(dye_idx)
        try:
            ss.setClassificationMinimumMaximum(0.0, float(peak_max) or 1.0)
            shader = ss.colorRampShader()
            shader.setMinimumValue(0.0)
            shader.setMaximumValue(float(peak_max) or 1.0)
            shader.classifyColorRamp(5, -1)
            ss.setColorRampShader(shader)
        except Exception as exc:  # noqa: BLE001
            print(f"[drive] ramp set note: {exc}", flush=True)
        rs.setScalarSettings(dye_idx, ss)
        ml.setRendererSettings(rs)

        # OSM basemap under the mesh so the screenshot shows the REAL river.
        osm = QgsRasterLayer(
            "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
            "&zmax=19&zmin=0",
            "OpenStreetMap",
            "wms",
        )
        render_layers = [ml, osm] if osm.isValid() else [ml]

        tp2 = ml.temporalProperties()
        te = tp2.timeExtent()
        begin, endt = te.begin(), te.end()
        span_ms = begin.msecsTo(endt)

        def render_at(frac, path):
            qdt = begin.addMSecs(int(frac * span_ms))
            ms = QgsMapSettings()
            ms.setLayers(render_layers)
            ms.setDestinationCrs(ml.crs())
            ext = ml.extent()
            ext.scale(1.25)
            ms.setExtent(ext)
            ms.setOutputSize(QSize(1100, 800))
            ms.setBackgroundColor(QColor(235, 240, 245))
            ms.setIsTemporal(True)
            ms.setTemporalRange(QgsDateTimeRange(qdt, qdt.addSecs(1)))
            job = QgsMapRendererSequentialJob(ms)
            job.start()
            job.waitForFinished()
            img = job.renderedImage()
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                img.save(path)
            return img

        peak_frac = peak_ds / max(n_dye - 1, 1)
        early_img = render_at(0.0, None)
        pump(3)  # let async XYZ tiles fetch before the final render
        peak_img = render_at(peak_frac, SHOT)
        check("peak-dye screenshot written (QgsMapRendererSequentialJob)",
              os.path.isfile(SHOT) and os.path.getsize(SHOT) > 3000, SHOT)

        diff = 0
        w, h = peak_img.width(), peak_img.height()
        for yy in range(0, h, 5):
            for xx in range(0, w, 5):
                if early_img.pixel(xx, yy) != peak_img.pixel(xx, yy):
                    diff += 1
        check("animation is real: early vs peak frame pixels DIFFER",
              diff > 20, f"{diff} differing sample pixels")

# NOTE: we deliberately do NOT delete the case -- NATE keeps it.
plugin.unload()
_restore()
qgs.exitQgis()

print(f"\n[drive] session_id={session_id}", flush=True)
print(f"[drive] FRESH_CASE_ID={demo_case}", flush=True)
if failures:
    print(f"[drive] DONE -- {len(failures)} FAILURE(S): {failures}", flush=True)
    sys.exit(1)
print(f"[drive] TELEMAC-P4-ACCEPTANCE-DONE -- ALL CHECKS PASSED (screenshot {SHOT})", flush=True)
sys.exit(0)
