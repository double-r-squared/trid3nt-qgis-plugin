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
  6. MARKDOWN (feature 2026-07-13): assistant answers stream PLAIN
     (an unclosed ``` fence mid-stream never hits the markdown parser),
     convert to rendered rich text on turn-complete and on replay; a TALL
     markdown message (header + paragraphs + json code block + table +
     list) must paint at its full wrapped height at BOTH a narrow (320px)
     and a wide (640px) dock width -- the F36 _WrapLabel min-height
     re-assert must hold for rich text too. User bubbles and the thinking
     block stay Qt.PlainText.

Exits 0 and prints DOCK-UI-OK plus the measured heights; asserts (nonzero)
otherwise. Also grabs docs/proof/90-dock-ui-batch.png and the markdown
proof docs/proof/93-dock-markdown.png (offscreen QWidget grabs -- LAYOUT
proofs, not pixel-parity claims vs live QGIS rendering).
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

# ---- 6. MARKDOWN (feature 2026-07-13): stream plain, finalize rich --------- #

from qgis.PyQt.QtCore import Qt  # noqa: E402

MD = (
    "### Flood summary\n\n"
    "The **peak depth** is *2.4 m* near the outfall, concentrated in the "
    "low-lying blocks east of the channel where the DEM dips below the "
    "2-year stage.\n\n"
    "1. Depth raster added to the case\n"
    "2. Velocity raster added to the case\n\n"
    "```json\n"
    "{\n"
    '  "depth_m": 2.4,\n'
    '  "cells": 120000,\n'
    '  "solver": "sfincs"\n'
    "}\n"
    "```\n\n"
    "| Layer | Max | Units |\n"
    "|-------|-----|-------|\n"
    "| Depth | 2.4 | m |\n"
    "| Velocity | 1.1 | m/s |\n\n"
    "Inline `run_solver` reference and a [docs link](https://example.com)."
)

# Close out section 5's still-pending entry through the real terminal seam.
dock._on_event("turn-complete", {})
assert dock._pending is None, "turn-complete did not close the pending entry"

dock._add_user_bubble("summarize the flood run")
md_entry = dock._ensure_pending()
# Stream in two chunks with an UNCLOSED ``` fence in between -- mid-stream
# text must stay plain (never parsed as markdown) and must not crash.
split = MD.index('"cells"')
dock._on_event("chunk", {"delta": MD[:split]})
pump()
assert md_entry.label.textFormat() == Qt.PlainText, (
    "mid-stream label left PlainText (markdown parsed an unclosed fence)"
)
assert md_entry.label.isVisible(), "streamed text not visible mid-stream"
dock._on_event("chunk", {"delta": MD[split:]})
pump()
assert md_entry.label.textFormat() == Qt.PlainText, (
    "label converted before turn-complete"
)
dock._on_event("turn-complete", {})
pump()
assert dock._pending is None, "turn-complete did not close the markdown entry"
assert md_entry.label.textFormat() == Qt.RichText, (
    "turn-complete did not convert the answer to rich markdown"
)
html = md_entry.label.text()
assert "<table" in html, "markdown table missing from the rendered HTML"
assert "background-color:" in html, "code-block background styling missing"
# The code background must be the REAL theme Base color, visibly distinct
# from the bubble grey -- regression guard: the bubble stylesheet polishes
# the LABEL's palette (Base -> the bubble background), so generating from
# label.palette() produced an invisible code background.
from qgis.PyQt.QtGui import QPalette  # noqa: E402

container_pal = md_entry.container.palette()
base_name = container_pal.color(QPalette.Base).name()
midlight_name = container_pal.color(QPalette.Midlight).name()
assert f"background-color:{base_name}" in html, (
    f"code background is not the theme Base color ({base_name}); "
    "was it generated from the stylesheet-polluted label palette?"
)
assert base_name != midlight_name, (
    "offscreen palette degenerate -- Base equals Midlight, test is void"
)
assert "monospace" in html, "monospace code styling missing"
assert not md_entry.label.openExternalLinks(), "openExternalLinks must stay off"
assert md_entry.label.textInteractionFlags() == Qt.TextSelectableByMouse, (
    "interaction flags changed -- links must not be activatable"
)

# The dock-integrated entry must also be unclipped at its ACTUAL width
# (a floating QDockWidget cannot shrink below its header row's minimum,
# so the narrow/wide sweep below uses the bare message-list stack).
md_h_dock = md_entry.label.height()
md_hfw_dock = md_entry.label.heightForWidth(md_entry.label.width())
assert md_hfw_dock > 0, "heightForWidth broke for rich text"
assert md_h_dock >= md_hfw_dock, (
    f"tall markdown clipped in the dock: {md_h_dock} < {md_hfw_dock}"
)

# Wrap-height sweep at EXACT 320px / 640px widths: replicate the dock's
# message-list stack (QScrollArea widgetResizable -> host QVBoxLayout with
# terminal stretch -> _AssistantEntry) without the dock header, whose
# button row forces a ~620px minimum on a floating dock offscreen.
from qgis.PyQt.QtWidgets import QScrollArea, QVBoxLayout, QWidget  # noqa: E402

mhost = QWidget()
mlay = QVBoxLayout(mhost)
mlay.setContentsMargins(2, 2, 2, 2)
mlay.setSpacing(4)
mlay.addStretch(1)
mscroll = QScrollArea()
mscroll.setWidgetResizable(True)
mscroll.setWidget(mhost)
mscroll.resize(320, 700)
mscroll.show()
sweep_entry = _AssistantEntry(mlay)
sweep_entry.append_delta(MD)
sweep_entry.finalize_markdown()
pump(20)
md_h_narrow = sweep_entry.label.height()
md_w_narrow = sweep_entry.label.width()
md_hfw_narrow = sweep_entry.label.heightForWidth(md_w_narrow)
print(
    f"[markdown] narrow(320px view): label {md_w_narrow}px wide, "
    f"height={md_h_narrow}, needed(heightForWidth)={md_hfw_narrow}"
)
assert md_hfw_narrow > 0, "heightForWidth broke for rich text (narrow)"
assert md_h_narrow >= md_hfw_narrow, (
    f"tall markdown clipped at narrow width: {md_h_narrow} < {md_hfw_narrow}"
)
# The label must WRAP at the narrow width, not force the scroll host wider
# (rich-text QLabels report the document's ideal unwrapped width as their
# minimumSizeHint -- regression guard for finalize_markdown's
# setMinimumWidth(1) cap).
assert md_w_narrow <= 320, (
    f"markdown label overflowed the 320px view: {md_w_narrow}px wide"
)

mscroll.resize(640, 700)
pump(20)
md_h_wide = sweep_entry.label.height()
md_w_wide = sweep_entry.label.width()
md_hfw_wide = sweep_entry.label.heightForWidth(md_w_wide)
print(
    f"[markdown] wide(640px view): label {md_w_wide}px wide, "
    f"height={md_h_wide}, needed(heightForWidth)={md_hfw_wide}"
)
assert md_h_wide >= md_hfw_wide, (
    f"tall markdown clipped at wide width: {md_h_wide} < {md_hfw_wide}"
)
# The finalized bubble must actually USE the wider view (regression guard
# for the ~217px wrapped-sizeHint pin the AlignLeft cell caused).
assert md_w_wide > md_w_narrow, (
    f"markdown label did not widen with the view: {md_w_narrow} -> {md_w_wide}"
)
# And RE-WRAP there: heightForWidth clamps to minimumHeight, so before the
# _WrapLabel clear-then-measure fix the min-height ratcheted at the narrow
# height forever (narrow->wide left a dead-space block instead of reflowing).
assert md_h_wide < md_h_narrow, (
    f"markdown label did not reflow when widened: {md_h_narrow} -> {md_h_wide}"
)
mscroll.hide()

# Replay path (case open): final text -> rendered markdown immediately.
replay_before = dock.messages_layout.count()
dock._replay_chat_history(
    [
        {"role": "user", "content": "what did the run find?"},
        {"role": "agent", "content": MD},
    ]
)
pump()
assert dock.messages_layout.count() == replay_before + 2, "replay rows missing"
replay_container = dock.messages_layout.itemAt(
    dock.messages_layout.count() - 2
).widget()
replay_labels = [
    l for l in replay_container.findChildren(QLabel)
    if l.textFormat() == Qt.RichText
]
assert replay_labels, "replayed assistant message was not markdown-rendered"

# User bubble + thinking block stay PLAIN text.
user_lbl = next(
    l for l in dock.messages_host.findChildren(QLabel)
    if l.text() == "summarize the flood run"
)
assert user_lbl.textFormat() == Qt.PlainText, "user bubble must stay plain text"
think_entry = _AssistantEntry(dock.messages_layout)
think_entry.append_thinking_delta("**not** markdown, raw musing")
think_entry.append_delta("Done.")
think_entry.finalize_markdown()
pump()
assert think_entry._thinking_label.textFormat() == Qt.PlainText, (
    "thinking block must stay plain text"
)
assert think_entry.label.textFormat() == Qt.RichText, "finalize skipped the answer"
print("[markdown] stream-plain -> finalize-rich, replay rich, user/thinking plain")

# ---- 7. NATE 2026-07-19 chat-UI batch (N1 fold / N2 tree / N3 state / ------- #
#         N4 collapsed-progress / N5 sim-card ordering) ----------------------- #

from trid3nt.dock import SimCard  # noqa: E402
from trid3nt.trid3nt_client import PipelineStep  # noqa: E402

# N3 (chip color = state) + N2 (nested child = tree connector): a chip's
# border/text color tracks the step state and a child row carries the ASCII
# "|->" connector.
chip_entry = _AssistantEntry(dock.messages_layout)
chip_entry.set_pipeline_rows(
    [
        {"chip": "fetch_dem", "detail": "complete", "state": "complete"},
        {"chip": "build_mesh", "detail": "running", "state": "running"},
        {"chip": "run_solver", "detail": "failed", "state": "failed"},
        {"chip": "sub_fetch", "detail": "complete", "state": "complete",
         "indent": True},
    ]
)
pump()
chip_styles = {}
connector_seen = False
for i in range(chip_entry.pipeline_area.count()):
    holder = chip_entry.pipeline_area.itemAt(i).widget()
    if holder is None:
        continue
    for lbl in holder.findChildren(QLabel):
        if lbl.text() == "|->":
            connector_seen = True
        else:
            chip_styles[lbl.text()] = lbl.styleSheet()
assert "#3fb950" in chip_styles.get("fetch_dem", ""), "complete chip not green"
assert "#8b949e" in chip_styles.get("build_mesh", ""), "running chip not grey"
assert "#f85149" in chip_styles.get("run_solver", ""), "failed chip not red"
assert "#3fb950" in chip_styles.get("sub_fetch", ""), "child chip color wrong"
assert connector_seen, "nested child row missing the |-> tree connector"
print("[N3/N2] chip state colors green/grey/red + child tree connector")

# N1: a RUNNING SimCard folds + unfolds at ANY time (not just terminal).
sim = SimCard("TELEMAC")
dock.messages_layout.insertWidget(dock.messages_layout.count() - 1, sim)
pump()
assert not sim.terminal, "fresh sim card should be non-terminal"
assert sim.details_toggle.isVisible(), "toggle not visible on a running card"
assert sim._body.isVisible(), "running card body should start expanded"
sim.details_toggle.setChecked(False)
sim._toggle_details(False)
pump()
assert not sim._body.isVisible(), "could not collapse a RUNNING card"
sim.details_toggle.setChecked(True)
sim._toggle_details(True)
pump()
assert sim._body.isVisible(), "could not re-expand a RUNNING card"
print("[N1] running sim card folds + unfolds anytime")

# N4: collapsed/expanded card shows live progress on the RIGHT.
sim.update_from_progress(
    {"run_id": "r1", "elapsed_seconds": 90, "phase": "routing",
     "active_cell_count": 12000}
)
pump()
assert "1:30" in sim.progress_lbl.text(), (
    f"live progress readout missing elapsed: {sim.progress_lbl.text()!r}"
)
assert "routing" in sim.progress_lbl.text(), "live progress readout missing phase"
print(f"[N4] live progress readout on the right: {sim.progress_lbl.text()!r}")

# N4/N1: terminal transition auto-collapses + shows the final duration.
sim.update_from_step(
    PipelineStep(
        step_id="s-sim", name="telemac:solve",
        tool_name="telemac_river_dye:solve", state="complete", role="compute",
        batch_job_id="local-docker:r1", duration_ms=165000,
    )
)
pump()
assert sim.terminal, "sim card did not go terminal"
assert not sim._body.isVisible(), "terminal card did not auto-collapse"
assert "complete" in sim.summary_lbl.text().lower(), "terminal summary wrong"
assert "2:45" in sim.progress_lbl.text(), (
    f"terminal duration missing from readout: {sim.progress_lbl.text()!r}"
)
print("[N4] terminal card: auto-collapsed, duration on the right")

# N5: a sim-card insert closes the streaming entry so narration lands BELOW.
dock._add_user_bubble("run the dye sim")
sim_pre = _AssistantEntry(dock.messages_layout)
dock._pending = sim_pre
sim_pre.append_delta("Dispatching the solver now.")
dock._route_compute_step(
    PipelineStep(
        step_id="s-order", name="telemac:solve",
        tool_name="telemac_river_dye:solve", state="running", role="compute",
        batch_job_id="local-docker:r2",
    )
)
pump()
assert dock._pending is None, "sim card did not close the pending entry"
sim_post = dock._ensure_pending()
sim_post.append_delta("Solver is running.")
pump()
inserted = dock._sim_cards.get("s-order")
assert inserted is not None, "sim card not registered"
i_pre = dock.messages_layout.indexOf(sim_pre.container)
i_card = dock.messages_layout.indexOf(inserted)
i_post = dock.messages_layout.indexOf(sim_post.container)
print(f"[N5] sim order pre={i_pre} card={i_card} post={i_post}")
assert 0 <= i_pre < i_card < i_post, (
    f"sim card out of order: pre={i_pre} card={i_card} post={i_post}"
)
print("[N5] sim card lands inline: pre-entry -> card -> post-entry")

# ---- proof screenshots (layout proof, not pixel parity with live QGIS) ----- #

os.makedirs(PROOF_DIR, exist_ok=True)
shot = os.path.join(PROOF_DIR, "90-dock-ui-batch.png")
pump(20)
dock.grab().save(shot)
print(f"[proof] offscreen dock grab -> {shot}")

# Dedicated markdown proof: a clean dock with one user bubble + the rendered
# markdown reply (header, bold, list, json code block, table, inline code).
mdock = Trid3ntDock(FakeIface())
mdock._auto_connect_done_this_show = True
mdock.resize(420, 760)
mdock.show()
mdock._add_user_bubble("summarize the flood run")
mdock._replay_chat_history([{"role": "agent", "content": MD}])
pump(20)
md_shot = os.path.join(PROOF_DIR, "93-dock-markdown.png")
mdock.grab().save(md_shot)
print(f"[proof] offscreen markdown grab -> {md_shot}")

print(
    "DOCK-UI-OK "
    f"bubble1line={short_h} bubble6line={long_h} needed={long_hfw} "
    f"assistant={a_h}/{a_hfw} "
    f"markdown-narrow={md_h_narrow}/{md_hfw_narrow}@{md_w_narrow}px "
    f"markdown-wide={md_h_wide}/{md_hfw_wide}@{md_w_wide}px"
)
sys.exit(0)
