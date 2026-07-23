"""Qt harness for the ADR 0018 tool-selection picker card (Stage 3, 2026-07-22).

Run as a SUBPROCESS by ``test_tool_picker.TestToolPickerQt`` -- it needs
``qgis.PyQt`` (PyQt5), which the pure-python test venv does not have; the
test probes the system interpreter and skips honestly when absent (the same
convention as ``qt_dock_ui_harness.py``).

Offscreen, no agent, no network. Checks (the card contract, ADR 0018 +
the fixed interface contract):

  1. RENDER: a ``tool-candidates`` event paints ONE ToolCandidatesCard with
     the stage label in the title, one radio per ranked candidate (tool name
     monospace + one-line summary), the free-text line edit as the LAST
     option, and Confirm / Let-agent-decide buttons. The card closes out the
     pending entry (BUG-4/N5 ordering -- post-decision narration lands
     BELOW the card).
  2. PICK: select a candidate radio + Confirm -> EXACTLY ONE
     ``send_tool_choice(request_id, tool_name, None)`` through the bridge
     hook; the card locks (single-answer -- a second Confirm is a no-op) and
     folds to the chip "picked <tool>" (prefixed "Step N: " -- LANE P
     2026-07-22 wave-picker UX; every card in this harness lands in the same
     never-reset turn, so the chips run Step 1..4 in creation order). A
     later turn event must NOT re-fold an answered card to "agent
     proceeded".
  3. FREE TEXT: typing selects the free-text radio; Confirm sends
     ``(None, stripped_text)`` and folds to the guidance chip.
  4. EMPTY-CONFIRM HONESTY: Confirm with nothing selected consumes NO
     decision (no send, not locked, honest note) -- mirroring the
     credential card's empty-Submit rule.
  5. LET AGENT DECIDE: sends ``(None, None)`` and folds to "agent decided".
  6. UNANSWERED FOLD: a subsequent turn event (chunk) arriving while the
     card is open folds it to "agent proceeded", locked, with NO reply sent
     (the server's timeout_s fail-open already resolved the selection).
  7. MALFORMED: a request without request_id paints NO card, only the
     honest error note.
  8. MODE TOGGLE: the Settings dialog's Auto/Ask combo persists
     ``settings.tool_choice_mode`` on Save, and the dock's send path stamps
     it onto ``bridge.send_chat(tool_choice_mode=...)``.

Exits 0 and prints TOOL-PICKER-OK; asserts (nonzero) otherwise.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

# Never touch the real QGIS profile's QSettings from this harness.
QCoreApplication.setOrganizationName("trid3nt-tool-picker-harness")
QCoreApplication.setApplicationName("trid3nt-tool-picker-harness")

app = QApplication([])

from qgis.PyQt.QtWidgets import QRadioButton  # noqa: E402

from stub_server import (  # noqa: E402
    STUB_TOOL_CANDIDATES_REQUEST_ID,
    TOOL_CANDIDATES_ROW,
)
from trid3nt.ui.cards import ToolCandidatesCard  # noqa: E402
from trid3nt.ui.dock import Trid3ntDock  # noqa: E402
from trid3nt.ui.settings_dialog import SettingsDialog  # noqa: E402


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
dock.resize(360, 700)
dock.show()
pump()

# Hook the bridge's picker-reply verb: every send lands here, none on a wire.
tool_choice_sends = []
dock.bridge.send_tool_choice = (
    lambda request_id, tool_name=None, free_text=None: tool_choice_sends.append(
        (request_id, tool_name, free_text)
    )
)


def picker_cards():
    return dock.messages_host.findChildren(ToolCandidatesCard)


# ---- 1. RENDER ------------------------------------------------------------- #

dock._on_event("chunk", {"delta": "Let me pick the right tool."})
pump()
pre_pending = dock._pending
assert pre_pending is not None, "no streaming entry before the card"

dock._on_event("tool-candidates", TOOL_CANDIDATES_ROW)
pump()
cards = picker_cards()
assert len(cards) == 1, f"expected 1 picker card, got {len(cards)}"
card = cards[0]
assert card.request_id == STUB_TOOL_CANDIDATES_REQUEST_ID
assert dock._pending is None, (
    "card must close out the pending entry (BUG-4/N5 ordering)"
)
assert dock._open_tool_pickers == [card], "card not tracked as open"

radios = card.findChildren(QRadioButton)
# 3 candidates + the free-text "Other:" radio, in ranked order then Other.
assert len(radios) == 4, f"expected 4 radios, got {len(radios)}"
expected_names = [c["tool_name"] for c in TOOL_CANDIDATES_ROW["candidates"]]
assert [r.text() for r in radios[:3]] == expected_names, (
    f"candidate radios out of order: {[r.text() for r in radios]}"
)
assert radios[3] is card.free_radio and card.free_radio.text() == "Other:"
assert not card._summary_container.isVisible(), "chip must start hidden"
assert card._body.isVisible(), "body must start expanded"
print("[render] card ok: stage title + 3 candidate radios + free-text last")

# ---- 2. PICK + single-answer lock + no re-fold ----------------------------- #

card._candidate_radios[0][0].setChecked(True)
card.confirm_btn.click()
pump()
assert tool_choice_sends == [
    (STUB_TOOL_CANDIDATES_REQUEST_ID, "spatial_query", None)
], f"pick send wrong: {tool_choice_sends}"
assert card.answered
assert not card.confirm_btn.isEnabled(), "card must lock after answering"
# Wave-picker UX (LANE P, 2026-07-22): every picker this harness shows lands
# in the SAME (never-_send-reset) turn, so the chip carries the running
# "Step N" prefix -- this is card 1 of the sequence below.
assert card.summary_lbl.text() == "Step 1: picked spatial_query", card.summary_lbl.text()
assert card._summary_container.isVisible() and not card._body.isVisible()
card.confirm_btn.click()  # locked -- a second answer must not send
card.decide_btn.click()
pump()
assert len(tool_choice_sends) == 1, "single-answer lock violated"

# A later turn event must NOT re-fold an ANSWERED card to "agent proceeded".
dock._on_event("chunk", {"delta": "Running spatial_query."})
pump()
assert card.summary_lbl.text() == "Step 1: picked spatial_query", (
    "answered chip was clobbered by the supersede sweep"
)
assert dock._open_tool_pickers == [], "answered card must leave the open list"
print("[pick] one send + lock + chip 'picked spatial_query' ok")

# ---- 3. FREE TEXT ---------------------------------------------------------- #

dock._on_event("tool-candidates", TOOL_CANDIDATES_ROW)
pump()
card2 = [c for c in picker_cards() if not c.answered][0]
# Simulate real typing: setText + the textEdited signal (typing selects the
# free-text radio so guidance is never attributed to a candidate).
card2.free_edit.setText("  summarize the building layer instead  ")
card2.free_edit.textEdited.emit(card2.free_edit.text())
assert card2.free_radio.isChecked(), "typing must select the free-text radio"
card2.confirm_btn.click()
pump()
assert tool_choice_sends[-1] == (
    STUB_TOOL_CANDIDATES_REQUEST_ID,
    None,
    "summarize the building layer instead",
), f"free-text send wrong: {tool_choice_sends[-1]}"
assert card2.summary_lbl.text() == "Step 2: sent guidance to the agent"
print("[free-text] typed guidance send + chip ok")

# ---- 4. EMPTY-CONFIRM HONESTY ---------------------------------------------- #

dock._on_event("tool-candidates", TOOL_CANDIDATES_ROW)
pump()
card3 = [c for c in picker_cards() if not c.answered][0]
n_before = len(tool_choice_sends)
card3.confirm_btn.click()  # nothing selected
pump()
assert len(tool_choice_sends) == n_before, "empty Confirm must not send"
assert not card3.answered, "empty Confirm must not consume the decision"
assert card3.result_lbl.isVisible(), "empty Confirm must show the honest note"

# ---- 5. LET AGENT DECIDE --------------------------------------------------- #

card3.decide_btn.click()
pump()
assert tool_choice_sends[-1] == (STUB_TOOL_CANDIDATES_REQUEST_ID, None, None)
assert card3.summary_lbl.text() == "Step 3: agent decided"
print("[decide] let-agent-decide send + chip 'agent decided' ok")

# ---- 6. UNANSWERED FOLD ---------------------------------------------------- #

dock._on_event("tool-candidates", TOOL_CANDIDATES_ROW)
pump()
card4 = [c for c in picker_cards() if not c.answered][0]
n_before = len(tool_choice_sends)
# The turn moves on (server timeout_s fail-open) -- the card must fold.
dock._on_event("chunk", {"delta": "No answer -- proceeding with spatial_query."})
pump()
assert card4.answered, "unanswered card must fold when the turn moves on"
assert card4.summary_lbl.text() == "Step 4: agent proceeded", card4.summary_lbl.text()
assert not card4.confirm_btn.isEnabled(), "superseded card must lock"
assert len(tool_choice_sends) == n_before, "superseded card must NOT reply"
assert dock._open_tool_pickers == []
print("[timeout] unanswered card folded to 'agent proceeded', no send")

# ---- 7. MALFORMED ----------------------------------------------------------- #

n_cards = len(picker_cards())
dock._on_event("tool-candidates", {"stage_label": "Data step"})  # no request_id
pump()
assert len(picker_cards()) == n_cards, "malformed request must not mint a card"

# ---- 8. MODE TOGGLE --------------------------------------------------------- #

# Point the Save-time provider-config push at a dead port so the harness
# never touches a live agent (the off-thread task errors silently).
dock.settings.export_api = "http://127.0.0.1:1"
dock.settings.tool_choice_mode = "auto"
dlg = SettingsDialog(dock.settings, dock)
assert dlg.tool_choice_combo.currentText() == "auto"
dlg.tool_choice_combo.setCurrentText("ask")
dlg.accept()
pump()
assert dock.settings.tool_choice_mode == "ask", "Save must persist the mode"

# The dock's send path stamps the persisted mode onto send_chat.
chat_sends = []


class FakeBridge:
    running = True

    def send_chat(self, text, show_thinking=False, model_id="",
                  aoi_bbox=None, tool_choice_mode=""):
        chat_sends.append((text, tool_choice_mode))


dock.bridge = FakeBridge()
dock._case_id = "01HARNESSCASEAAAAAAAAAAAAA"
dock.input_edit.setPlainText("what tools would you use here")
dock._send()
pump()
assert chat_sends == [("what tools would you use here", "ask")], chat_sends
print("[mode] Settings Save persisted 'ask' and _send stamped it on the wire")

print("TOOL-PICKER-OK")
sys.exit(0)
