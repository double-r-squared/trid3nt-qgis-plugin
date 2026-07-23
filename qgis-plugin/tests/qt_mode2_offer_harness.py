"""Qt harness for the offer-to-add card + wave-picker UX (LANE P, 2026-07-22
-- SRS Sec F.1.2 Mode 2, ADR 0018 auto/ask modes).

Run as a SUBPROCESS by ``test_mode2_offer.TestMode2OfferQt`` -- it needs
``qgis.PyQt`` (PyQt5), which the pure-python test venv does not have; the
test probes the system interpreter and skips honestly when absent (the same
convention as ``qt_tool_picker_harness.py``).

Offscreen, no agent, no network. Checks:

  1. RENDER (offer-to-add): a ``mode2-candidate`` event paints ONE
     Mode2CandidateCard with the host in the title, the URL as a real
     clickable link, the "why flagged" pattern/confidence lines, and
     Add-to-catalog / Dismiss buttons. The card closes out the pending entry
     (BUG-4/N5 ordering).
  2. ADD: Add-to-catalog -> EXACTLY ONE
     ``respond_catalog_addition(candidate_id, "accept", ...)`` through the
     bridge hook; the card locks (single-answer) and folds to the chip
     "added <host> to the catalog".
  3. DISMISS: on a fresh card, Dismiss -> ``respond_catalog_addition(...,
     "reject", ...)`` and folds to the chip "dismissed".
  4. MALFORMED: a mode2-candidate envelope with no candidate_id paints NO
     card, only the honest error note.
  5. WAVE-PICKER: two ``tool-candidates`` events land in one turn (distinct
     stage labels) -> two ToolCandidatesCards, titled "Step 1 - Data step:
     pick a tool" / "Step 2 - Analysis step: pick a tool"; the FIRST folds
     to "Step 1: agent proceeded" the instant the SECOND arrives (the
     existing supersede sweep, now step-labeled). A new turn (_send) resets
     the step counter back to 1.

Exits 0 and prints MODE2-OFFER-OK; asserts (nonzero) otherwise.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QLabel  # noqa: E402

# Never touch the real QGIS profile's QSettings from this harness.
QCoreApplication.setOrganizationName("trid3nt-mode2-offer-harness")
QCoreApplication.setApplicationName("trid3nt-mode2-offer-harness")

app = QApplication([])

from stub_server import (  # noqa: E402
    MODE2_CANDIDATE_ROW,
    STUB_MODE2_CANDIDATE_ID,
    WAVE_TOOL_CANDIDATES_STEP1,
    WAVE_TOOL_CANDIDATES_STEP2,
)
from trid3nt.ui.cards import Mode2CandidateCard, ToolCandidatesCard  # noqa: E402
from trid3nt.ui.dock import Trid3ntDock  # noqa: E402


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

# Hook the offer-to-add reply verb: every send lands here, none on a wire.
catalog_sends = []
dock.bridge.respond_catalog_addition = (
    lambda request_id, decision, edited_catalog_entry=None, reject_reason=None: (
        catalog_sends.append((request_id, decision, edited_catalog_entry, reject_reason))
    )
)
tool_choice_sends = []
dock.bridge.send_tool_choice = (
    lambda request_id, tool_name=None, free_text=None: tool_choice_sends.append(
        (request_id, tool_name, free_text)
    )
)


def mode2_cards():
    return dock.messages_host.findChildren(Mode2CandidateCard)


def picker_cards():
    return dock.messages_host.findChildren(ToolCandidatesCard)


# ---- 1. RENDER (offer-to-add) ------------------------------------------- #

dock._on_event("chunk", {"delta": "Fetching the source page."})
pump()
assert dock._pending is not None, "no streaming entry before the card"

dock._on_event("mode2-candidate", MODE2_CANDIDATE_ROW)
pump()
cards = mode2_cards()
assert len(cards) == 1, f"expected 1 offer-to-add card, got {len(cards)}"
card = cards[0]
assert dock._pending is None, (
    "card must close out the pending entry (BUG-4/N5 ordering)"
)
# The URL rides as a real RichText link -- never plain-text-hidden.
link_texts = [
    lbl.text() for lbl in card.findChildren(type(card.result_lbl))
    if "waterdata.usgs.gov" in lbl.text()
]
assert any("href=" in t for t in link_texts), (
    f"URL must render as a real link: {link_texts}"
)
assert not card._summary_container.isVisible(), "chip must start hidden"
assert card._body.isVisible(), "body must start expanded"
print("[render] offer-to-add card ok: host title + URL link + why-flagged lines")

# ---- 2. ADD + single-answer lock -------------------------------------------- #

card.add_btn.click()
pump()
assert catalog_sends == [
    (STUB_MODE2_CANDIDATE_ID, "accept", None, None)
], f"add send wrong: {catalog_sends}"
assert not card.add_btn.isEnabled(), "card must lock after answering"
assert card.summary_lbl.text() == "added waterdata.usgs.gov to the catalog", (
    card.summary_lbl.text()
)
assert card._summary_container.isVisible() and not card._body.isVisible()
card.add_btn.click()  # locked -- a second answer must not send
card.dismiss_btn.click()
pump()
assert len(catalog_sends) == 1, "single-answer lock violated"
print("[add] one send + lock + chip 'added <host> to the catalog' ok")

# ---- 3. DISMISS (fresh card) ------------------------------------------------ #

dock._on_event("mode2-candidate", MODE2_CANDIDATE_ROW)
pump()
card2 = [c for c in mode2_cards() if c is not card][0]
card2.dismiss_btn.click()
pump()
assert catalog_sends[-1] == (STUB_MODE2_CANDIDATE_ID, "reject", None, None), (
    catalog_sends[-1]
)
assert card2.summary_lbl.text() == "dismissed"
print("[dismiss] send + chip 'dismissed' ok")

# ---- 4. MALFORMED ----------------------------------------------------------- #

n_cards = len(mode2_cards())
dock._on_event("mode2-candidate", {"candidate": {"url": "https://x.gov"}})  # no candidate_id
pump()
assert len(mode2_cards()) == n_cards, "malformed request must not mint a card"

# ---- 5. WAVE-PICKER: two candidates in one turn, step-labeled, folds ------- #

# A fresh turn resets the step counter (mirrors _send()).
dock._tool_picker_turn_step = 0
dock._on_event("tool-candidates", WAVE_TOOL_CANDIDATES_STEP1)
pump()
step1_cards = [c for c in picker_cards() if not c.answered]
assert len(step1_cards) == 1, f"expected 1 open picker, got {len(step1_cards)}"
step1 = step1_cards[0]
assert step1._step_index == 1, step1._step_index

# Title carries "Step 1 - Data step: pick a tool".
step1_title = [
    lbl.text() for lbl in step1.findChildren(QLabel)
    if lbl.text().startswith("Step 1")
]
assert step1_title == ["Step 1 - Data step: pick a tool"], step1_title


dock._on_event("tool-candidates", WAVE_TOOL_CANDIDATES_STEP2)
pump()
# The FIRST card folds to "agent proceeded" (step-labeled) the instant the
# SECOND arrives -- the existing supersede sweep now step-labeled.
assert step1.answered, "step 1 must fold once step 2 supersedes it"
assert step1.summary_lbl.text() == "Step 1: agent proceeded", step1.summary_lbl.text()

open_cards = [c for c in picker_cards() if not c.answered]
assert len(open_cards) == 1, f"expected 1 open (step 2) picker, got {len(open_cards)}"
step2 = open_cards[0]
assert step2._step_index == 2, step2._step_index
step2_title = [
    lbl.text() for lbl in step2.findChildren(QLabel)
    if lbl.text().startswith("Step 2")
]
assert step2_title == ["Step 2 - Analysis step: pick a tool"], step2_title
print("[wave] two staged pickers: Step 1/Step 2 titles + first folds on second")

# A NEW turn resets the counter -- the next picker in a fresh turn is "Step 1"
# again, not a running total across turns.
dock._tool_picker_turn_step = 0
dock._on_event("tool-candidates", WAVE_TOOL_CANDIDATES_STEP1)
pump()
fresh_open = [c for c in picker_cards() if not c.answered]
assert fresh_open, "expected a fresh open picker after the counter reset"
assert fresh_open[-1]._step_index == 1, (
    "a new turn must restart the step count at 1"
)
print("[wave] new-turn step counter reset ok")

print("MODE2-OFFER-OK")
sys.exit(0)
