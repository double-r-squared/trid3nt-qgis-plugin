"""Live re-drive proof: stale-AOI fix (Twin Falls, Idaho groundwater/dye demo).

Loads the REAL Trid3nt plugin inside an offscreen QgsApplication (same pattern
as headless_oq_chart_proof.py), connects to the LIVE local agent on
ws://127.0.0.1:8765/ws, creates a FRESH case with the AOI toggle OFF, and drives
three natural-language turns (NO bbox coordinates in the prompt):

  T1  Model groundwater flow with a pumping well near Twin Falls, Idaho.
  T2  Now simulate a contaminant tracer released upgradient of the well and
      show how the plume spreads over 10 years.
  T3  Which areas does the plume reach, and what is the peak concentration?

Any solver-confirm / granularity gate is auto-confirmed PROMPTLY (the turn
timer runs during gates). Screenshots (dock-widget grabs) land at
docs/proof/105-dye-t1-head.png, 106-dye-t2-plume.png, 107-dye-t3-analysis.png.

The PASS/FAIL of the stale-AOI fix is judged from logs/agent.log after the run
(the tool-call aoi_latlon / spill_location args must be near Twin Falls, Idaho,
NOT Chattanooga). This script only DRIVES + screenshots; coord assertion is done
by the caller reading the log for this session_id, which the script prints.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_dye_redrive_proof.py
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402
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

PROOF_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)
SHOTS = {
    1: os.path.join(PROOF_DIR, "105-dye-t1-head.png"),
    2: os.path.join(PROOF_DIR, "106-dye-t2-plume.png"),
    3: os.path.join(PROOF_DIR, "107-dye-t3-analysis.png"),
}

PROMPTS = {
    1: "Model groundwater flow with a pumping well near Twin Falls, Idaho.",
    2: (
        "Now simulate a contaminant tracer released upgradient of the well and "
        "show how the plume spreads over 10 years."
    ),
    3: (
        "Which areas does the plume reach, and what is the peak concentration?"
    ),
}

qgs = QgsApplication([], False)
qgs.initQgis()


class FakeIface:
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

from trid3nt.plugin import Trid3ntPlugin  # noqa: E402
from trid3nt.ui.cards import GateCard  # noqa: E402

_qs = QSettings()
_prior = {
    k: _qs.value(k, None)
    for k in (
        "trid3nt/local_url",
        "trid3nt/anonymous_user_id",
        "trid3nt/canvas_aoi",
    )
}
_qs.setValue("trid3nt/local_url", AGENT_URL)
_qs.setValue("trid3nt/anonymous_user_id", ANON_USER_ID)
# AOI toggle OFF -- the whole point: the place name alone must geocode.
_qs.setValue("trid3nt/canvas_aoi", "false")
_qs.sync()


def _restore():
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
        _auto_confirm_gates()
        time.sleep(0.05)


_EVENTS = {"turn_complete": 0}


def _observe_event(kind, data):
    """Additional slot on bridge.agent_event -- counts terminal turn-complete
    events so drive_turn can wait on the REAL end of a turn (first-token
    latency + long sims mean text-quiet heuristics fire far too early)."""
    if kind == "turn-complete":
        _EVENTS["turn_complete"] += 1
        print(f"[drive] <turn-complete #{_EVENTS['turn_complete']}>", flush=True)
    elif kind == "payload-warning":
        print("[drive] <payload-warning received>", flush=True)


def _auto_confirm_gates():
    """Answer any open GateCard promptly with its default (proceed) decision."""
    for card in dock.messages_host.findChildren(GateCard):
        if card._decided is None:
            print(
                f"[drive] auto-confirming gate warning_id={card._warning.warning_id}",
                flush=True,
            )
            card._proceed()


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

# Settle past the startup-reuse's own case-open before creating our fresh case.
pump(15)
startup_case = dock._case_id
print(f"[drive] settled startup_case={startup_case}", flush=True)

# --------------------------------------------------------------------------- #
# Fresh case, AOI OFF -- must NOT inherit any prior AOI anchor.
# --------------------------------------------------------------------------- #
dock.new_case()
pump(30, lambda: dock._case_id is not None and dock._case_id != startup_case)
pump(4)
demo_case = dock._case_id
print(f"[drive] FRESH_CASE_ID={demo_case} title={dock._case_title!r}", flush=True)
print(f"[drive] canvas_aoi setting = {dock.settings.canvas_aoi}", flush=True)

# --------------------------------------------------------------------------- #
# Drive three turns. Each: type prompt, send, pump while auto-confirming gates.
# --------------------------------------------------------------------------- #
TURN_BUDGET = {1: 600, 2: 900, 3: 420}  # generous; sims can run minutes


def drive_turn(n):
    print(f"\n[drive] === TURN {n} SEND @ {time.strftime('%H:%M:%S')} ===", flush=True)
    print(f"[drive] prompt: {PROMPTS[n]}", flush=True)
    t0 = time.time()
    baseline = _EVENTS["turn_complete"]
    dock.input_edit.setText(PROMPTS[n])
    dock._send()
    # Wait for the REAL end of the turn: a fresh turn-complete event beyond the
    # baseline. Auto-confirm any gate promptly throughout (the turn timer runs
    # during gates). Hard-cap at the per-turn budget so a wedged turn cannot
    # hang the drive forever.
    budget = TURN_BUDGET[n]
    end = time.time() + budget
    last_beat = 0.0
    while time.time() < end:
        QCoreApplication.processEvents()
        _auto_confirm_gates()
        if _EVENTS["turn_complete"] > baseline:
            break
        now = time.time()
        if now - last_beat >= 30:
            last_beat = now
            print(
                f"[drive]   turn {n} in progress +{now - t0:.0f}s "
                f"(text_len={_pending_len()})",
                flush=True,
            )
        time.sleep(0.2)
    elapsed = time.time() - t0
    done = _EVENTS["turn_complete"] > baseline
    print(
        f"[drive] === TURN {n} {'COMPLETE' if done else 'BUDGET-CAP'} "
        f"after {elapsed:.0f}s ===",
        flush=True,
    )
    # Let trailing events (notes/layers/zoom) settle before the screenshot.
    pump(4)
    # Screenshot the dock (widget grab renders fine offscreen).
    os.makedirs(PROOF_DIR, exist_ok=True)
    dock.resize(460, 900)
    pump(1)
    dock.grab().save(SHOTS[n])
    print(f"[drive] screenshot: {SHOTS[n]}", flush=True)
    return elapsed


def _pending_len():
    p = getattr(dock, "_pending", None)
    if p is None:
        return -1
    try:
        return len(getattr(p, "text", "") or "")
    except Exception:
        return -2


timings = {}
for n in (1, 2, 3):
    timings[n] = drive_turn(n)

print("\n[drive] === DONE ===", flush=True)
print(f"[drive] FRESH_CASE_ID={demo_case}", flush=True)
print(f"[drive] final_case_title={dock._case_title!r}", flush=True)
for n in (1, 2, 3):
    print(f"[drive] turn{n}_elapsed_s={timings[n]:.0f}", flush=True)
_client = getattr(dock.bridge, "client", None)
print(f"[drive] session_id={getattr(_client, 'session_id', None)}", flush=True)

plugin.unload()
_restore()
qgs.exitQgis()
print("[drive] DYE-REDRIVE-DONE", flush=True)
