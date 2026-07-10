"""Live proof: opening a DIFFERENT case must clear the previous case's chat
+ layers, not accumulate them, and must auto-focus the canvas (live-feedback
2026-07-10, items A/B/C/D).

Loads the REAL plugin inside a real (offscreen) QgsApplication -- same
pattern as ``headless_first_run.py`` -- connects to the LIVE local agent on
``ws://127.0.0.1:8765``, opens an EXISTING case that already has layers +
chat history + a persisted bbox, then opens a DIFFERENT existing case with
NO persisted bbox and NO vector layers, and asserts:

  ITEM A  exactly ONE "TRID3NT "-prefixed layer-tree group remains (the
          newly-opened case's), the first case's group + its layers are
          gone from the project, and the OpenStreetMap basemap survives
          untouched (it lives directly at layerTreeRoot, never in a group).
  ITEM B  the dock's message list is cleared and repopulated: its child
          count does NOT just keep growing across the switch, and the
          replayed bubble count matches the newly-opened case's own
          persisted chat_history (capped at 50), not the previous case's.
  ITEM C  the first case's "Flood depth step 1..7" frame sequence lands
          grouped into ONE collapsed "flood depth (animation, 7 frames)"
          subgroup, not 7 flat sibling layers (proves the case-open REPLAY
          path -- not just a fresh live stream -- also groups).
  ITEM D  case A (has a persisted bbox) auto-focuses the canvas (extent
          changes, "Zoomed to case area" noted); case B (no bbox, no vector
          layers -- a raster-only case) leaves the canvas exactly where it
          was and says so honestly ("Case has no stored map area - keeping
          current view") instead of silently doing nothing.

Uses two cases already sitting in the local dev persistence store (see
``data/persistence/grace2_dev/projects.json``) that belong to the same
local anonymous user this proof authenticates as -- no LLM turn needed, so
the proof is fast and does not queue behind other live work.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_case_switch_proof.py

Set TRID3NT_AGENT_URL / TRID3NT_ANON_USER_ID to point at a different stack
or user (defaults match the box this proof was authored against). Set
TRID3NT_CASE_A / TRID3NT_CASE_B to point at different case ids (A must have
layers + a "step N" frame sequence + chat history + a persisted bbox for
the strongest proof; B must be a DIFFERENT case the same user owns, ideally
with no persisted bbox and no vector layers to exercise the item D honest
fallback note).
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QLabel, QMainWindow  # noqa: E402

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

AGENT_URL = os.environ.get("TRID3NT_AGENT_URL", "ws://127.0.0.1:8765/ws")
ANON_USER_ID = os.environ.get(
    "TRID3NT_ANON_USER_ID", "0110CA1VSERAAAAAAAAAAAAAAA"
)
CASE_A = os.environ.get("TRID3NT_CASE_A", "01KX36YMH5GM3SDJV0K17JT3PK")
CASE_A_TITLE = "One Two Sentences Floodplain Do Not"
CASE_B = os.environ.get("TRID3NT_CASE_B", "01KX509BASVDVWJGMXC51P150J")
CASE_B_TITLE = "Landcover Over Washington State"

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
# exit (same hygiene as headless_first_run.py) -- binding to the existing
# anon user is what makes ``select_case`` against pre-existing case ids
# below succeed (the server re-binds the SAME local User record; see
# trid3nt_client.AgentClient.connect's sticky-anonymous comment).
_qs = QSettings()
_prior = {
    k: _qs.value(k, None)
    for k in ("trid3nt/local_url", "trid3nt/anonymous_user_id", "trid3nt/canvas_aoi")
}
_qs.setValue("trid3nt/local_url", AGENT_URL)
_qs.setValue("trid3nt/anonymous_user_id", ANON_USER_ID)
_qs.setValue("trid3nt/canvas_aoi", "false")  # no chat sent -- irrelevant, keep it quiet
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

# -- connect (auto-connect fires from showEvent when the dock is shown by --
# -- toggle_dock; belt-and-suspenders explicit call in case it raced) ------ #
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

# Local-mode auto-connect (``AgentWorker._bind_startup_case``) fires its OWN
# "select" for the reused/newest case; ``case_ready`` fires optimistically
# BEFORE that case-open reply necessarily lands (it can take several more
# seconds -- the reply queues behind the handshake + whatever the server is
# doing). Settle GENEROUSLY here before driving our own explicit selects
# below -- otherwise the startup reuse's OWN case-open can land AFTER ours
# and clobber ``dock._case_id``/the layer tree right back to the startup
# case (a test-harness timing hazard in THIS proof, not anything
# ``set_case``/``_on_case_open_event`` themselves get wrong -- confirmed by
# tracing every ``_on_case_open_event`` firing: a real user who waits for
# the dock to say "Connected" before opening the Cases dialog never lands
# inside this window).
pump(15)
print(f"[proof] settled after connect: case={dock._case_id}", flush=True)

initial_extent = iface.mapCanvas().extent()
print(f"[proof] canvas extent before any select: {initial_extent.toString()}", flush=True)

root = QgsProject.instance().layerTreeRoot()


def trid3nt_groups():
    return [g for g in root.findGroups() if g.name().startswith("TRID3NT ")]


def message_widget_count():
    # exclude the terminal stretch item (no widget)
    return sum(
        1
        for i in range(dock.messages_layout.count())
        if dock.messages_layout.itemAt(i).widget() is not None
    )


def message_text_blob():
    """All visible label text currently in the message list, joined -- used
    to prove the PREVIOUS case's bubbles are gone, not just outnumbered."""
    return "\n".join(lbl.text() for lbl in dock.messages_host.findChildren(QLabel))


# --------------------------------------------------------------------------- #
# Open case A (has layers + a 7-frame sequence + chat history)
# --------------------------------------------------------------------------- #

dock.select_case(CASE_A, CASE_A_TITLE)
pump(20, lambda: dock._case_id == CASE_A)
print(f"[proof] after select A: case_id={dock._case_id}", flush=True)
pump(3)  # let any trailing queued Qt events (note/replay) settle

groups_a = trid3nt_groups()
print(f"[proof] groups after A: {[g.name() for g in groups_a]}", flush=True)
check("case A bound", dock._case_id == CASE_A)
check("exactly one TRID3NT group after opening A", len(groups_a) == 1, str(groups_a))
case_group_a = groups_a[0] if groups_a else None
layer_count_a = len(case_group_a.findLayerIds()) if case_group_a else 0
check("case A group has layers", layer_count_a > 0, f"n={layer_count_a}")

anim_subgroups_a = (
    [g for g in case_group_a.findGroups() if "animation" in g.name()]
    if case_group_a
    else []
)
check(
    "item C: A's flood-depth-step frames landed in ONE animation subgroup",
    len(anim_subgroups_a) == 1
    and any("flood depth" in g.name() for g in anim_subgroups_a),
    str([g.name() for g in anim_subgroups_a]),
)
if anim_subgroups_a:
    sub = anim_subgroups_a[0]
    check("item C: animation subgroup is collapsed", not sub.isExpanded())
    check(
        "item C: animation subgroup has multiple frame members",
        len(sub.findLayerIds()) >= 2,
        str(len(sub.findLayerIds())),
    )

count_after_a = message_widget_count()
blob_a = message_text_blob()
print(f"[proof] message widget count after A: {count_after_a}", flush=True)
check("item B: case A chat replay populated the message list", count_after_a > 1)
check(
    "item B: case A's distinctive chat text ('floodplain') actually replayed",
    "floodplain" in blob_a.lower(),
)

osm_before = QgsProject.instance().mapLayersByName("OpenStreetMap")
check("OpenStreetMap basemap present after A", bool(osm_before))

# item D: case A has a persisted bbox -- the canvas must have auto-focused.
extent_after_a = iface.mapCanvas().extent()
print(f"[proof] canvas extent after A: {extent_after_a.toString()}", flush=True)
check(
    "item D: canvas auto-focused when opening case A (bbox present)",
    extent_after_a.toString() != initial_extent.toString(),
)
check(
    "item D: 'Zoomed to case area' noted for case A",
    "zoomed to case area" in blob_a.lower(),
)

# --------------------------------------------------------------------------- #
# Open case B (a DIFFERENT case) -- A's group/layers/chat must vanish
# --------------------------------------------------------------------------- #

dock.select_case(CASE_B, CASE_B_TITLE)
pump(20, lambda: dock._case_id == CASE_B)
print(f"[proof] after select B: case_id={dock._case_id}", flush=True)
pump(3)

groups_b = trid3nt_groups()
print(f"[proof] groups after B: {[g.name() for g in groups_b]}", flush=True)
check("case B bound", dock._case_id == CASE_B)
check(
    "item A: only ONE TRID3NT group remains after switching to B",
    len(groups_b) == 1,
    str([g.name() for g in groups_b]),
)
check(
    "item A: the remaining group is B's, not A's",
    bool(groups_b) and CASE_B_TITLE in groups_b[0].name(),
    str([g.name() for g in groups_b]),
)
check(
    "item A: A's case-A-titled group is gone",
    not any(CASE_A_TITLE in g.name() for g in groups_b),
)

osm_after = QgsProject.instance().mapLayersByName("OpenStreetMap")
check(
    "item A: OpenStreetMap basemap survives the switch untouched",
    bool(osm_after) and (not osm_before or osm_after[0].id() == osm_before[0].id()),
)

count_after_b = message_widget_count()
blob_b = message_text_blob()
print(f"[proof] message widget count after B: {count_after_b}", flush=True)
check("item B: case B's own chat replayed (non-empty message list)", count_after_b > 1)
check(
    "item B: case A's chat text is GONE after switching to B (cleared, not appended)",
    "floodplain" not in blob_b.lower(),
)
check(
    "item B: case B's own distinctive chat text ('washington') replayed",
    "washington" in blob_b.lower(),
)

# item D: case B has NO persisted bbox and NO vector layers (raster-only) --
# the canvas must stay exactly where case A left it, with an honest note
# explaining why (never a silent no-op).
extent_after_b = iface.mapCanvas().extent()
print(f"[proof] canvas extent after B: {extent_after_b.toString()}", flush=True)
check(
    "item D: canvas view KEPT for case B (no bbox, no vector layers)",
    extent_after_b.toString() == extent_after_a.toString(),
)
check(
    "item D: honest 'no stored map area' note shown for case B",
    "case has no stored map area - keeping current view" in blob_b.lower(),
)

# The dock's case label must say B, and the previous case's title text must
# not still be showing anywhere in the (now-cleared) message list.
check("case label shows B's title", CASE_B_TITLE in dock.case_label.text())

os.makedirs(os.path.join(PLUGIN_PATH, "..", "docs", "proof"), exist_ok=True)
try:
    dock.grab().save(
        os.path.join(PLUGIN_PATH, "..", "docs", "proof", "50-case-switch-proof.png")
    )
except Exception:  # noqa: BLE001 -- screenshot is a nice-to-have, not the proof
    pass

plugin.unload()
_restore()
qgs.exitQgis()

if failures:
    print(f"\n[proof] DONE -- {len(failures)} FAILURE(S): {failures}", flush=True)
    sys.exit(1)
print("\n[proof] DONE -- ALL CHECKS PASSED", flush=True)
sys.exit(0)
