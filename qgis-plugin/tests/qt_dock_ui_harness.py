"""Regression harness for the 2026-07-12 dock UI fix batch.

Run as a SUBPROCESS by ``test_dock_ui.TestDockUiBatch`` -- it needs
``qgis.PyQt`` (PyQt5), which the pure-python test venv does not have; the
test probes the system interpreter and skips honestly when absent (the same
convention as ``qt_bridge_harness.py``).

Offscreen, no agent, no network. Checks:

  1. BUG 1 (wrapped-bubble clip): a long user message at a narrow dock
     width must paint at its FULL wrapped height (height >=
     heightForWidth(actual width)); pre-fix it clipped to one visual line
     (measured 73px painted vs 133px needed at 320px). Same check for the
     assistant answer label.
  2. BUG 2 (empty assistant bubble): whitespace-only text deltas after a
     thinking block must NOT reveal the answer label nor collapse the
     thinking block; the first NON-whitespace delta does both.
  3. BUG 3a (Layers (N) collapse): a layer-note batch folds into one
     default-collapsed toggle; error notes stay visible outside it.
  4. BUG 3b (probe panel): probe output goes to the pinned panel, replaced
     in place, and never adds a widget to the chat message list.
  5. BUG 4 (gate-card ordering): a gate card closes out the streaming
     entry; post-decision output lands in a NEW entry BELOW the card
     (user bubble -> pre-gate entry -> card -> post-gate entry).

Exits 0 and prints DOCK-UI-OK plus the measured heights; asserts (nonzero)
otherwise. Also grabs docs/proof/90-dock-ui-batch.png (offscreen QWidget
grab -- a LAYOUT proof, not a pixel-parity claim vs live QGIS rendering).
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

# Never touch the real QGIS profile's QSettings from this harness.
QCoreApplication.setOrganizationName("trid3nt-dock-ui-harness")
QCoreApplication.setApplicationName("trid3nt-dock-ui-harness")

app = QApplication([])

from trid3nt.dock import GateCard, Trid3ntDock, _AssistantEntry  # noqa: E402

PROOF_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)


class FakeIface:
    """Headless iface: no canvas (mapCanvas raises -> the dock's documented
    headless no-op paths), no active layer."""

    def mapCanvas(self):
        raise RuntimeError("headless harness has no canvas")

    def activeLayer(self):
        return None


def pump(n: int = 10) -> None:
    for _ in range(n):
        QCoreApplication.processEvents()


dock = Trid3ntDock(FakeIface())
dock._auto_connect_done_this_show = True  # block showEvent auto-connect
dock.resize(320, 700)
dock.show()
pump()

# ---- 1. BUG 1: wrapped user bubble paints at full height ------------------- #

SHORT = "hi"
LONG = (
    "show me the landcover over washington state and also tell me which "
    "classes dominate the eastern half versus the western half of the state"
)
dock._add_user_bubble(SHORT)
dock._add_user_bubble(LONG)
pump()

user_labels = []
from qgis.PyQt.QtWidgets import QLabel  # noqa: E402

for lbl in dock.messages_host.findChildren(QLabel):
    if lbl.text() in (SHORT, LONG):
        user_labels.append(lbl)
assert len(user_labels) == 2, f"expected 2 user labels, got {len(user_labels)}"
short_lbl = next(l for l in user_labels if l.text() == SHORT)
long_lbl = next(l for l in user_labels if l.text() == LONG)
short_h, long_h = short_lbl.height(), long_lbl.height()
long_hfw = long_lbl.heightForWidth(long_lbl.width())
print(
    f"[bug1] 1-line bubble height={short_h}  long bubble height={long_h} "
    f"needed(heightForWidth@{long_lbl.width()}px)={long_hfw}"
)
assert long_h >= long_hfw, f"long bubble clipped: {long_h} < {long_hfw}"
assert long_h > short_h, "long bubble did not grow past the 1-line height"

entry = _AssistantEntry(dock.messages_layout)
entry.append_delta(
    "Evergreen forest dominates western Washington while shrub, scrub and "
    "cultivated crops dominate the east of the state, with alpine classes "
    "along the Cascades crest. " * 2
)
pump()
a_h = entry.label.height()
a_hfw = entry.label.heightForWidth(entry.label.width())
print(f"[bug1] assistant label height={a_h} needed={a_hfw}")
assert a_h >= a_hfw, f"assistant label clipped: {a_h} < {a_hfw}"

# ---- 2. BUG 2: whitespace-only deltas never reveal the bubble -------------- #

ws_entry = _AssistantEntry(dock.messages_layout)
ws_entry.append_thinking_delta("planning the tool calls for this turn")
pump()
ws_entry.append_delta("\n")
ws_entry.append_delta("  \n\n")
pump()
assert not ws_entry.label.isVisible(), "whitespace-only delta revealed the bubble"
assert ws_entry._thinking_toggle.isChecked(), (
    "whitespace-only delta collapsed the thinking block"
)
assert ws_entry._thinking_toggle.text() == "Thinking...", (
    "whitespace-only delta relabeled the thinking toggle"
)
print("[bug2] whitespace-only tail: label hidden, thinking still expanded")

ws_entry.append_delta("Here is the answer.")
pump()
assert ws_entry.label.isVisible(), "non-whitespace delta did not reveal the bubble"
assert ws_entry.label.text() == "Here is the answer.", (
    f"leading whitespace not trimmed for display: {ws_entry.label.text()!r}"
)
assert not ws_entry._thinking_toggle.isChecked(), (
    "first non-whitespace delta did not collapse the thinking block"
)
print("[bug2] first non-whitespace delta: bubble shown, thinking collapsed")

# ---- 3. BUG 3a: Layers (N) collapse, errors stay outside ------------------- #

layer_entry = _AssistantEntry(dock.messages_layout)
layer_entry.add_layer_notes(
    [
        "Added raster layer 'DEM (USGS 3DEP)'",
        "Added raster layer 'Landcover (NLCD 2021)'",
        "layer 'Broken': failed (RuntimeError: bad COG)",
    ]
)
pump()
assert layer_entry._layers_toggle.isVisible(), "Layers toggle not shown"
assert layer_entry._layers_toggle.text() == "Layers (2)", (
    f"bad toggle label: {layer_entry._layers_toggle.text()!r}"
)
assert not layer_entry._layers_body.isVisible(), "Layers body not collapsed by default"
assert layer_entry.notes_area.count() == 1, "error note was swallowed by the collapse"
err_lbl = layer_entry.notes_area.itemAt(0).widget()
assert "failed" in err_lbl.text() and err_lbl.isVisible(), "error note not visible"
layer_entry._layers_toggle.setChecked(True)
layer_entry._toggle_layer_notes()
pump()
assert layer_entry._layers_body.isVisible(), "expanding the Layers toggle failed"
inner = layer_entry._layers_body_lay.count()
assert inner == 2, f"expected 2 collapsed lines, got {inner}"
layer_entry.add_layer_notes(["Added vector layer 'Rivers'"])
assert layer_entry._layers_toggle.text() == "Layers (3)", "second batch did not extend N"
layer_entry._layers_toggle.setChecked(False)
layer_entry._toggle_layer_notes()
pump()
print("[bug3a] Layers (N) collapse: default collapsed, errors outside, batches extend")

# ---- 4. BUG 3b: probe output pinned to the panel, never in chat ------------ #

chat_count_before = dock.messages_layout.count()
assert not dock._probe_panel.isVisible(), "probe panel visible before any probe"
dock._set_probe_output("Probing 122.33W, 47.61N ...")
pump()
assert dock._probe_panel.isVisible(), "probe panel did not appear"
dock._set_probe_output(
    "Probe 122.33W, 47.61N:\n  DEM (USGS 3DEP): 132.4 m\n  Landcover: 42 "
    "(evergreen forest)"
)
pump()
assert "132.4" in dock.probe_result_label.text(), "probe result not replaced in place"
assert dock.messages_layout.count() == chat_count_before, (
    "probe output added a widget to the chat message list"
)
dock.probe_results_toggle.setChecked(False)
dock._toggle_probe_results()
pump()
assert not dock.probe_result_label.isVisible(), "probe collapse toggle failed"
dock.probe_results_toggle.setChecked(True)
dock._toggle_probe_results()
pump()
print("[bug3b] probe panel: in-place updates, collapsible, zero chat widgets")

# ---- 5. BUG 4: gate card sits between pre- and post-gate entries ----------- #

dock._add_user_bubble("run the flood sim")
pre_entry = _AssistantEntry(dock.messages_layout)
dock._pending = pre_entry  # what _send does
pre_entry.append_delta("Planning the run -- this needs your confirmation.")
dock._show_gate_card(
    {
        "warning_id": "w-harness-1",
        "tool_name": "run_solver",
        "estimated_mb": 120,
        "threshold_mb": 50,
        "recommendation": "confirm the resolution before running",
    }
)
pump()
assert dock._pending is None, "gate card did not close out the pending entry"
post_entry = dock._ensure_pending()
post_entry.append_delta("Confirmed -- starting the run.")
pump()
cards = dock.messages_host.findChildren(GateCard)
assert len(cards) == 1, f"expected 1 gate card, got {len(cards)}"
i_pre = dock.messages_layout.indexOf(pre_entry.container)
i_card = dock.messages_layout.indexOf(cards[0])
i_post = dock.messages_layout.indexOf(post_entry.container)
print(f"[bug4] layout order pre-entry={i_pre} card={i_card} post-entry={i_post}")
assert 0 <= i_pre < i_card < i_post, (
    f"gate card out of order: pre={i_pre} card={i_card} post={i_post}"
)

# ---- proof screenshot (layout proof, not pixel parity with live QGIS) ------ #

os.makedirs(PROOF_DIR, exist_ok=True)
shot = os.path.join(PROOF_DIR, "90-dock-ui-batch.png")
pump(20)
dock.grab().save(shot)
print(f"[proof] offscreen dock grab -> {shot}")

print(
    "DOCK-UI-OK "
    f"bubble1line={short_h} bubble6line={long_h} needed={long_hfw} "
    f"assistant={a_h}/{a_hfw}"
)
sys.exit(0)
