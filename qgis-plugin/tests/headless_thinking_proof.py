"""Headless live proof: F9 thinking stream in the QGIS dock.

Boots the plugin in an offscreen QgsApplication, connects to the LIVE local
agent (ws://127.0.0.1:8765), sends a no-tool prompt with the show_thinking
setting ON (the default), and asserts the dock's collapsible thinking block
receives streamed reasoning text. Screenshots to docs/proof/.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_thinking_proof.py

Same manual live-proof class as headless_first_run.py (not part of make
test); needs the local stack up. ASCII hyphens only; no emojis.
"""

from __future__ import annotations

import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

PROOF = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)
PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

qgs = QgsApplication([], False)
qgs.initQgis()


class FakeIface:
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


def pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.02)


# Preserve the profile's anonymous_user_id / url the same way first_run does.
settings = QSettings()
saved_uid = settings.value("trid3nt/anonymous_user_id", "")
saved_url = settings.value("trid3nt/local_url", "")
agent_url = os.environ.get("TRID3NT_AGENT_URL", "ws://127.0.0.1:8765")
settings.setValue("trid3nt/local_url", agent_url)
settings.setValue("trid3nt/canvas_aoi", "false")
# guard: drop a poisoned non-ULID uid so the live agent accepts the handshake
uid = str(saved_uid or "")
if uid and not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", uid):
    settings.remove("trid3nt/anonymous_user_id")
settings.sync()


def _restore():
    if saved_url:
        settings.setValue("trid3nt/local_url", saved_url)
    if saved_uid:
        settings.setValue("trid3nt/anonymous_user_id", saved_uid)
    settings.sync()


iface = FakeIface()
from trid3nt.plugin import Trid3ntPlugin  # noqa: E402

plugin = Trid3ntPlugin(iface)
plugin.initGui()
plugin.toggle_dock(True)
dock = plugin.dock
assert dock is not None
iface.mainWindow().show()
dock.connect_agent()

print(f"[thinking-proof] show_thinking={dock.settings.show_thinking}", flush=True)

# wait for connect + case bind (mirrors first_run)
t0 = time.time()
while time.time() - t0 < 60 and dock._case_id is None:
    pump(1)
print(
    f"[thinking-proof] connected={dock._connected} case={dock._case_id}",
    flush=True,
)
if dock._case_id is None:
    print("[thinking-proof] FAILED: no case bound (agent down?)", flush=True)
    plugin.unload()
    _restore()
    qgs.exitQgis()
    sys.exit(2)

dock.input_edit.setText(
    "In one or two sentences, what is a watershed? Do not use any tools."
)
dock._send()
print("[thinking-proof] prompt sent", flush=True)

# Poll the dock's pending assistant entry for a visible thinking block.
ok_thinking = False
ok_answer = False
seen_entries = []
t0 = time.time()
while time.time() - t0 < 240:
    pump(3)
    entry = getattr(dock, "_pending", None)
    if entry is not None and entry not in seen_entries:
        seen_entries.append(entry)
    for entry in seen_entries:
        cont = getattr(entry, "_thinking_container", None)
        text = getattr(entry, "_thinking_text", "") or ""
        if cont is not None and cont.isVisible() and len(text) > 0:
            ok_thinking = True
        body = entry.label.text() or ""
        if ok_thinking and len(body.strip()) > 20:
            ok_answer = True
    if ok_thinking and ok_answer:
        break

elapsed = time.time() - t0
print(
    f"[thinking-proof] thinking={ok_thinking} answer={ok_answer} "
    f"after {elapsed:.0f}s",
    flush=True,
)

os.makedirs(PROOF, exist_ok=True)
shot = os.path.join(PROOF, "45-qgis-thinking-stream.png")
iface.mainWindow().grab().save(shot)
print(f"[thinking-proof] screenshot -> {shot}", flush=True)

# collapse check: toggle off and confirm the label hides
if ok_thinking:
    for entry in seen_entries:
        cont = getattr(entry, "_thinking_container", None)
        if cont is not None and cont.isVisible():
            entry._thinking_toggle.setChecked(False)
            entry._toggle_thinking()
            pump(0.5)
            collapsed = not entry._thinking_label.isVisible()
            print(f"[thinking-proof] collapse works={collapsed}", flush=True)
            break

plugin.unload()
_restore()
qgs.exitQgis()
print(
    "PASS" if (ok_thinking and ok_answer) else "FAIL",
    f"thinking={ok_thinking} answer={ok_answer}",
    flush=True,
)
sys.exit(0 if (ok_thinking and ok_answer) else 1)
