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

from trid3nt.ui.cards import GateCard, _AssistantEntry  # noqa: E402
from trid3nt.ui.dock import Trid3ntDock  # noqa: E402

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

# ---- 3. T8 (NATE 2026-07-20): the "Layers (N)" toggle is GONE ------------- #
# The user sees rendered layers in the QGIS map / layer tree, so successful
# layer notes are dropped from chat; only FAILURE notes still surface (visible
# error lines). The materialization path itself is untouched (tested elsewhere).

layer_entry = _AssistantEntry(dock.messages_layout)
assert not hasattr(layer_entry, "_layers_toggle"), "Layers toggle should be removed"
layer_entry.add_layer_notes(
    [
        "Added raster layer 'DEM (USGS 3DEP)'",
        "Added raster layer 'Landcover (NLCD 2021)'",
        "layer 'Broken': failed (RuntimeError: bad COG)",
    ]
)
pump()
# Only the ONE failure note surfaces; the two successful notes are dropped.
# (F7: a single error note now lives inside an _ErrorFold widget -- N==1
# renders as the plain visible red line, so the check reads the inner label.)
assert layer_entry.notes_area.count() == 1, "expected only the error note to surface"
err_holder = layer_entry.notes_area.itemAt(0).widget()
err_lbls = [l for l in err_holder.findChildren(QLabel) if "failed" in l.text()]
assert err_lbls and err_lbls[0].isVisible(), "error note not visible"
layer_entry.add_layer_notes(["Added vector layer 'Rivers'"])  # success -> dropped
assert layer_entry.notes_area.count() == 1, "a successful note leaked into chat"
print("[T8] Layers toggle removed: successes dropped, failures still visible")

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

from trid3nt.ui.cards import SimCard  # noqa: E402
from trid3nt.net.trid3nt_client import PipelineStep  # noqa: E402

# T1..T5 (NATE 2026-07-20): the parent tool card. render_tool_card builds ONE
# _ToolCard whose inner rows keep the state-driven TEXT color (green/grey/red),
# each prefixed with ">" (T4), and carry a right-edge status glyph -- a spinner
# frame while running, a check on success, an x on failure (T5). While a row is
# still running the card stays EXPANDED (T3).
from trid3nt.ui.cards import _SPINNER_FRAMES, _STATUS_GLYPH_DONE, _STATUS_GLYPH_FAIL  # noqa: E402,E501

chip_entry = _AssistantEntry(dock.messages_layout)
chip_entry.render_tool_card(
    [
        {"label": "fetch_dem", "state": "complete", "nested": False},
        {"label": "build_mesh", "state": "running", "nested": False},
        {"label": "run_solver", "state": "failed", "nested": False},
        {"label": "sub_fetch", "state": "complete", "nested": True},
    ],
    ["fetch_dem: bbox=..."],
)
pump()
card = chip_entry._tool_card
assert card is not None, "render_tool_card did not create a parent _ToolCard"
assert card._body.isVisible(), "card body should be EXPANDED while a row runs (T3)"
name_styles = {}
prefix_seen = 0
glyphs = []
for i in range(card._body_lay.count()):
    holder = card._body_lay.itemAt(i).widget()
    if holder is None:
        continue
    labels = holder.findChildren(QLabel)
    texts = [lbl.text() for lbl in labels]
    if ">" in texts:
        prefix_seen += 1
    for lbl in labels:
        t = lbl.text()
        if t in ("fetch_dem", "build_mesh", "run_solver", "sub_fetch"):
            name_styles[t] = lbl.styleSheet()
        elif t in (_STATUS_GLYPH_DONE, _STATUS_GLYPH_FAIL) or t in _SPINNER_FRAMES:
            glyphs.append(t)
assert "#3fb950" in name_styles.get("fetch_dem", ""), "complete row not green"
assert "#8b949e" in name_styles.get("build_mesh", ""), "running row not grey"
assert "#f85149" in name_styles.get("run_solver", ""), "failed row not red"
assert prefix_seen == 4, f"expected a '>' prefix on all 4 rows, got {prefix_seen}"
assert _STATUS_GLYPH_DONE in glyphs, "no success check glyph"
assert _STATUS_GLYPH_FAIL in glyphs, "no failure x glyph"
assert any(g in _SPINNER_FRAMES for g in glyphs), "no running spinner glyph"
# T3: once every row is terminal the inner list AUTO-COLLAPSES (one shot).
chip_entry.render_tool_card(
    [
        {"label": "fetch_dem", "state": "complete", "nested": False},
        {"label": "build_mesh", "state": "complete", "nested": False},
    ],
    [],
)
pump()
assert not card._body.isVisible(), "card did not auto-collapse when all rows done (T3)"
card._toggle()  # the chevron re-expands it
pump()
assert card._body.isVisible(), "chevron re-expand failed"
print("[T1..T5] parent tool card: >-prefixed rows, state colors, glyphs, auto-collapse")

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

# ---- 8. Code-exec approval card (live-feedback 2026-07-21) ------------------ #
# The agent's code-exec-request envelope previously had ZERO handling (the
# agent blocked on its confirm gate forever). The dock must render the
# approval card inline, close out the streaming entry (BUG-4/N5 discipline),
# show the rationale + a COLLAPSED read-only verbatim code preview, and send
# the tool-payload-confirmation reply (warning_id == code_exec_id,
# revised_args None) on Run/Deny -- then fold to a one-line state chip.

from trid3nt.ui.cards import CodeExecCard, _ToolCard  # noqa: E402

sent_confirms = []
dock.bridge.confirm_payload = (
    lambda wid, dec, rev=None: sent_confirms.append((wid, dec, rev))
)

CODE_REQ = {
    "envelope_type": "code-exec-request",
    "code_exec_id": "01HARNESSCODEEXECAAAAAAAAA",
    "python_code": "result = 1 + 1\n",
    "layer_refs": {
        "depth": "s3://bucket/depth.tif",
        "frames": ["s3://a.tif", "s3://b.tif"],
    },
    "rationale": "Compute a sum over the depth layer.",
}
dock._add_user_bubble("what is the p95 depth?")
ce_pre = _AssistantEntry(dock.messages_layout)
dock._pending = ce_pre
ce_pre.append_delta("I will compute that with a short script.")
dock._on_event("code-exec-request", CODE_REQ)
pump()
assert dock._pending is None, "code-exec card did not close out the pending entry"
ce_cards = dock.messages_host.findChildren(CodeExecCard)
assert len(ce_cards) == 1, f"expected 1 code-exec card, got {len(ce_cards)}"
ce_card = ce_cards[0]
# Ordering: pre-entry -> card -> post-decision entry (BUG-4/N5 flow).
ce_post = dock._ensure_pending()
ce_post.append_delta("Waiting for your approval.")
pump()
i_pre = dock.messages_layout.indexOf(ce_pre.container)
i_card = dock.messages_layout.indexOf(ce_card)
i_post = dock.messages_layout.indexOf(ce_post.container)
assert 0 <= i_pre < i_card < i_post, (
    f"code-exec card out of order: pre={i_pre} card={i_card} post={i_post}"
)
# Collapsed read-only verbatim preview: hidden until toggled, never editable.
assert not ce_card.code_view.isVisible(), "code preview must start collapsed"
assert ce_card.code_view.isReadOnly(), "code preview must be read-only"
assert ce_card.code_view.toPlainText() == CODE_REQ["python_code"], (
    "code preview is not the verbatim python_code"
)
ce_card.code_toggle.click()
pump()
assert ce_card.code_view.isVisible(), "show-code toggle did not expand the preview"
assert ce_card.code_toggle.text() == "hide code", "toggle label did not flip"
# Rationale + layer lines render in the body.
from qgis.PyQt.QtWidgets import QLabel as _QLabel  # noqa: E402

ce_texts = [l.text() for l in ce_card.findChildren(_QLabel)]
assert any("Compute a sum" in t for t in ce_texts), "rationale missing from card"
assert any("depth: s3://bucket/depth.tif" in t for t in ce_texts), (
    "layer line missing from card"
)
assert any("frames: 2 frames" in t for t in ce_texts), (
    "multi-frame layer line missing from card"
)
# Run -> ONE confirmation (proceed, revised None), locked + folded to a chip.
ce_card.run_btn.click()
pump()
assert sent_confirms == [("01HARNESSCODEEXECAAAAAAAAA", "proceed", None)], (
    f"unexpected confirmation send: {sent_confirms!r}"
)
assert not ce_card.run_btn.isEnabled() and not ce_card.deny_btn.isEnabled(), (
    "buttons not disabled after the decision"
)
assert ce_card._summary_container.isVisible(), "answered card did not fold"
assert not ce_card._body.isVisible(), "answered card body still expanded"
assert "approved" in ce_card.summary_lbl.text().lower(), (
    f"summary chip wrong: {ce_card.summary_lbl.text()!r}"
)
ce_card._run()  # locked: answered exactly once, never a double send
assert len(sent_confirms) == 1, "locked card re-sent a confirmation"
# Deny path on a second card.
dock._on_event(
    "code-exec-request", dict(CODE_REQ, code_exec_id="01HARNESSCODEEXECBBBBBBBBB")
)
pump()
deny_card = [
    c for c in dock.messages_host.findChildren(CodeExecCard) if c is not ce_card
][0]
deny_card.deny_btn.click()
pump()
assert sent_confirms[-1] == ("01HARNESSCODEEXECBBBBBBBBB", "cancel", None), (
    f"deny did not send cancel: {sent_confirms[-1]!r}"
)
assert "denied" in deny_card.summary_lbl.text().lower(), "deny chip wrong"
# Malformed envelope: an honest error note, never a card / crash.
n_ce_cards = len(dock.messages_host.findChildren(CodeExecCard))
dock._on_event("code-exec-request", {"python_code": ""})
pump()
assert len(dock.messages_host.findChildren(CodeExecCard)) == n_ce_cards, (
    "malformed code-exec-request minted a card"
)
dock._on_event("turn-complete", {})
print("[code-exec] approval card: inline order, collapsed verbatim preview, "
      "Run=proceed / Deny=cancel via tool-payload-confirmation, lock + chip")

# ---- 9. F3: a no-tool turn mints ZERO tool cards ---------------------------- #
# Live-feedback 2026-07-21 ("empty stale tool card"): a pipeline frame whose
# steps are ALL filtered (LLM bookkeeping) used to lazily mint an empty
# "Tools" shell. A turn with zero tool events must leave zero tool cards.

n_toolcards_before = len(dock.messages_host.findChildren(_ToolCard))
dock._add_user_bubble("just answer in prose")
f3_entry = dock._ensure_pending()
dock._on_event(
    "pipeline",
    {
        "pipeline_id": "p-f3",
        "steps": [
            PipelineStep(
                step_id="s-llm",
                name="llm_generation",
                tool_name="llm_generation",
                state="running",
            )
        ],
    },
)
dock._on_event("chunk", {"delta": "No tools needed for this one."})
dock._on_event(
    "pipeline",
    {
        "pipeline_id": "p-f3",
        "steps": [
            PipelineStep(
                step_id="s-llm",
                name="llm_generation",
                tool_name="llm_generation",
                state="complete",
            )
        ],
    },
)
dock._on_event("turn-complete", {})
pump()
assert f3_entry._tool_card is None, "no-tool turn minted a tool card shell"
assert (
    len(dock.messages_host.findChildren(_ToolCard)) == n_toolcards_before
), "a stale empty tool card appeared on a no-tool turn"
# A REAL tool step still mints the card (the guard must not over-filter).
f3b_entry = dock._ensure_pending()
dock._on_event(
    "pipeline",
    {
        "pipeline_id": "p-f3b",
        "steps": [
            PipelineStep(
                step_id="s-real",
                name="fetch_elevation",
                tool_name="fetch_elevation",
                state="running",
            )
        ],
    },
)
pump()
assert f3b_entry._tool_card is not None, "a real tool step no longer mints a card"
dock._on_event("turn-complete", {})
print("[F3] no-tool turn minted zero tool cards; real tool step still mints")

# ---- 10. F4: tool-card border tracks the aggregate state -------------------- #
# Neutral (palette mid) while running, GREEN once every tool completed,
# RED when any failed/cancelled -- same palette as the row text colors.

f4_entry = _AssistantEntry(dock.messages_layout)
f4_entry.render_tool_card(
    [{"label": "fetch_dem", "state": "running", "nested": False}], []
)
pump()
f4_card = f4_entry._tool_card
assert f4_card is not None, "F4 card not minted"
assert "palette(mid)" in f4_card.styleSheet(), (
    f"running card lost the neutral border: {f4_card.styleSheet()!r}"
)
f4_entry.render_tool_card(
    [
        {"label": "fetch_dem", "state": "complete", "nested": False},
        {"label": "zonal_stats", "state": "complete", "nested": False},
    ],
    [],
)
pump()
assert "#3fb950" in f4_card.styleSheet(), (
    f"all-complete card border not green: {f4_card.styleSheet()!r}"
)
f4_entry.render_tool_card(
    [
        {"label": "fetch_dem", "state": "complete", "nested": False},
        {"label": "zonal_stats", "state": "failed", "nested": False},
    ],
    [],
)
pump()
assert "#f85149" in f4_card.styleSheet(), (
    f"failed card border not red: {f4_card.styleSheet()!r}"
)
print("[F4] tool-card border: neutral running -> green success -> red failure")

# ---- 11. F7: error notes wrap with the view + consecutive errors fold ------- #
# Live-feedback 2026-07-22: red error lines (the case-open rehydrate "MinIO
# fetch failed (http://...) -- skipped" notes) rendered statically sized --
# the unbroken presigned URL reported an unbreakable label width (measured
# sizeHint 400px pre-fix) that dragged the dock wider than its minimum. Fix
# (1): break-anywhere opportunities inside long unbroken tokens, so the line
# wraps and reflows with the view. Fix (2): consecutive error notes fold into
# ONE collapsed "ERRORS (N)" toggle row, expanding in place; a single error
# (N==1) stays a plain wrapped red line with no toggle chrome.

from trid3nt.ui.cards import _ErrorFold  # noqa: E402

URL_NOTE = (
    "vector 'Rivers & Streams': MinIO fetch failed "
    "(http://127.0.0.1:9000/grace2-cases/case-0123456789abcdef/layers/"
    "rivers-and-streams.fgb?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-"
    "Credential=minioadmin%2F20260722&X-Amz-Signature="
    "0123456789abcdef0123456789abcdef0123456789abcdef) -- skipped"
)

# (b) WRAPPING on the bare 320px message stack (the floating dock's header
# row forces a ~620px minimum offscreen -- same trick as the markdown sweep):
# the error line must reflow inside the narrow view and must NOT report a
# preferred width past the chat container (zero horizontal growth vs a plain
# text bubble; pre-fix the note label's sizeHint was 400px > the 320px view
# and the host sizeHint ballooned 145 -> 444px).
fhost = QWidget()
flay = QVBoxLayout(fhost)
flay.setContentsMargins(2, 2, 2, 2)
flay.setSpacing(4)
flay.addStretch(1)
fscroll = QScrollArea()
fscroll.setWidgetResizable(True)
fscroll.setWidget(fhost)
fscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
fscroll.resize(320, 700)
fscroll.show()
wrap_entry = _AssistantEntry(flay)
wrap_entry.append_delta("Baseline bubble text that wraps like any chat text.")
pump(20)
host_w_before = fhost.width()
wrap_entry.add_note(URL_NOTE, error=True)
pump(20)
view_w = fscroll.viewport().width()
single_fold = wrap_entry.notes_area.itemAt(0).widget()
assert isinstance(single_fold, _ErrorFold), (
    f"error note did not build an _ErrorFold: {type(single_fold).__name__}"
)
err_lbl = single_fold._body.findChildren(QLabel)[0]
print(
    f"[F7] narrow(320px view): error row sizeHint={single_fold.sizeHint().width()}px "
    f"label sizeHint={err_lbl.sizeHint().width()}px label {err_lbl.width()}px wide "
    f"x {err_lbl.height()}px tall, host sizeHint={fhost.sizeHint().width()}px"
)
assert single_fold.sizeHint().width() <= view_w, (
    f"error row prefers {single_fold.sizeHint().width()}px -- wider than the "
    f"{view_w}px chat container (would drag the dock wide)"
)
assert err_lbl.sizeHint().width() <= view_w, (
    f"error label prefers {err_lbl.sizeHint().width()}px > the {view_w}px view"
)
assert fhost.sizeHint().width() <= view_w, (
    f"host sizeHint grew past the container: {fhost.sizeHint().width()}px "
    f"(pre-fix ballooned to 444px)"
)
assert fhost.width() == host_w_before, (
    f"error note widened the chat stack: {host_w_before} -> {fhost.width()}"
)
assert err_lbl.width() <= view_w, "error label painted past the view"
# The unbroken-URL line actually WRAPPED (multi-line, not one clipped line).
assert err_lbl.height() >= 3 * err_lbl.fontMetrics().lineSpacing(), (
    f"URL note did not wrap: {err_lbl.height()}px tall at "
    f"{err_lbl.fontMetrics().lineSpacing()}px/line"
)

# (c) N==1 decision: a single error stays a plain wrapped red line -- the
# body is visible immediately and NO toggle chrome shows.
assert not single_fold.toggle.isVisible(), "N==1 must not show the ERRORS toggle"
assert single_fold._body.isVisible(), "single error line must be visible"
assert "#f85149" in err_lbl.styleSheet(), "error line lost the red accent"

# (a) FOLDING: N consecutive error notes -> ONE collapsed "ERRORS (N)" row,
# expanding in place to all N wrapped lines (charts-toggle affordance).
f7_entry = _AssistantEntry(dock.messages_layout)
for i in range(4):
    f7_entry.add_note(
        URL_NOTE.replace("Rivers & Streams", f"Layer {i}"), error=True
    )
pump()
assert f7_entry.notes_area.count() == 1, (
    f"4 consecutive errors left {f7_entry.notes_area.count()} note widgets -- "
    "expected ONE fold row"
)
fold = f7_entry.notes_area.itemAt(0).widget()
assert isinstance(fold, _ErrorFold), "consecutive errors did not fold"
assert fold.count == 4 and fold.toggle.text() == "ERRORS (4)", (
    f"fold header wrong: count={fold.count} text={fold.toggle.text()!r}"
)
assert fold.toggle.isVisible(), "ERRORS (N) toggle not visible for N=4"
assert "#f85149" in fold.toggle.styleSheet(), "fold toggle lost the red accent"
assert not fold._body.isVisible(), "fold must start COLLAPSED"
fold.toggle.click()  # expand in place
pump()
assert fold._body.isVisible(), "expand did not reveal the error lines"
fold_lbls = fold._body.findChildren(QLabel)
assert len(fold_lbls) == 4, f"expected 4 error lines in the fold, got {len(fold_lbls)}"
assert all(l.isVisible() for l in fold_lbls), "expanded fold hid some lines"
# A user-expanded fold stays expanded as the run continues; count updates.
f7_entry.add_note(URL_NOTE.replace("Rivers & Streams", "Layer 4"), error=True)
pump()
assert fold.count == 5 and fold.toggle.text() == "ERRORS (5)", "count did not update"
assert fold._body.isVisible(), "a new error collapsed a user-expanded fold"
# A STATUS note breaks the run: the next error starts a fresh fold (N==1 ->
# plain line again), inline below, in scroll order.
f7_entry.add_note("Case 'Harness' active")
f7_entry.add_note("raster 'DEM': COG fetch failed -- skipped", error=True)
pump()
assert f7_entry.notes_area.count() == 3, (
    f"expected fold + status + fresh fold, got {f7_entry.notes_area.count()}"
)
second_fold = f7_entry.notes_area.itemAt(2).widget()
assert isinstance(second_fold, _ErrorFold) and second_fold.count == 1, (
    "status note did not break the consecutive-error run"
)
assert not second_fold.toggle.isVisible(), "fresh N==1 fold grew toggle chrome"

# Persisted-history rendering (case reopen): consecutive role="error" rows
# fold exactly like live arrival -- same add_note path, same fold.
dock._on_event("turn-complete", {})
dock._replay_chat_history(
    [
        {"role": "error", "content": URL_NOTE},
        {"role": "error",
         "content": "vector 'Roads': MinIO fetch failed -- skipped"},
    ]
)
pump()
replay_entry = dock._pending
assert replay_entry is not None, "replayed errors minted no entry"
assert replay_entry.notes_area.count() == 1, "replayed errors did not fold"
replay_fold = replay_entry.notes_area.itemAt(0).widget()
assert isinstance(replay_fold, _ErrorFold) and replay_fold.count == 2, (
    "replayed consecutive errors not in ONE fold"
)
assert not replay_fold._body.isVisible(), "replayed fold must start collapsed"
assert replay_fold.toggle.text() == "ERRORS (2)", "replayed fold header wrong"
# Errors SEPARATED by conversation must not glue into one fold on replay.
dock._on_event("turn-complete", {})
dock._replay_chat_history(
    [
        {"role": "error", "content": "raster 'DEM': fetch failed -- skipped"},
        {"role": "agent", "content": "The rest of the case loaded fine."},
        {"role": "error", "content": "vector 'Roads': fetch failed -- skipped"},
    ]
)
pump()
sep_entry = dock._pending
assert sep_entry is not None and sep_entry.notes_area.count() == 2, (
    "conversation-separated errors should land in TWO folds"
)
sep_folds = [
    sep_entry.notes_area.itemAt(i).widget()
    for i in range(sep_entry.notes_area.count())
]
assert all(isinstance(f, _ErrorFold) and f.count == 1 for f in sep_folds), (
    "an agent row between errors did not break the fold run"
)
dock._on_event("turn-complete", {})
print(
    "[F7] error notes: wrap with the view (no width growth) + consecutive "
    "errors fold to ERRORS (N), N==1 stays a plain line, replay folds too"
)

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
