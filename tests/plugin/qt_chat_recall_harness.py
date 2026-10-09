"""Per-case chat recall through real key presses on the dock's composer.

Run as a SUBPROCESS by its wrapper, which needs ``qgis.PyQt`` and skips honestly
when absent. Offscreen, no agent: the bridge is a recorder. Covers UP on an
empty box recalling the last sent message, DOWN returning to an empty box, a
typed box keeping the arrows for the cursor, and each case keeping its own."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.PyQt.QtCore import QCoreApplication, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

QCoreApplication.setOrganizationName("trid3nt-chat-recall-harness")
QCoreApplication.setApplicationName("trid3nt-chat-recall-harness")

app = QApplication([])

from plugin.ui.dock import Trid3ntDock  # noqa: E402


class FakeIface:
    def mapCanvas(self):
        raise RuntimeError("headless harness has no canvas")

    def activeLayer(self):
        return None


class RecBridge:
    def __init__(self) -> None:
        self.sent: list = []
        self.running = True

    def send_chat(self, text, **_kw) -> None:
        self.sent.append(text)

    def send_dev_tool_invoke(self, name, args, raw_text="") -> None:
        self.sent.append(raw_text)

    def stop(self) -> None:
        pass


def _open(dock, case_id, user_rows):
    history = [{"role": "user", "content": t} for t in user_rows]
    dock._on_case_open_event({"session_state": {
        "case": {"case_id": case_id, "title": case_id},
        "chat_history": history}})


def _key(widget, key):
    QTest.keyClick(widget, key)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


dock = Trid3ntDock(FakeIface())
dock._auto_connect_done_this_show = True
dock.settings.auto_basemap = False
dock.bridge = RecBridge()
box = dock.input_edit

_open(dock, "A", ["first in A"])
box.setPlainText("!run build_mesh resolution=50")
dock._send()
_check(box.toPlainText() == "", "send left text in the box")

_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "!run build_mesh resolution=50",
       f"empty+Up did not recall the last sent: {box.toPlainText()!r}")
_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "first in A",
       f"second Up did not walk back to the persisted row: {box.toPlainText()!r}")
_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "first in A", "Up past the oldest moved off it")
_key(box, Qt.Key.Key_Down)
_check(box.toPlainText() == "!run build_mesh resolution=50", "Down did not walk forward")
_key(box, Qt.Key.Key_Down)
_check(box.toPlainText() == "", f"Down past the newest did not empty the box: {box.toPlainText()!r}")
print("RECALL-WALK-OK")

box.setPlainText("line one\nline two")
cursor = box.textCursor()
cursor.movePosition(cursor.MoveOperation.End)
box.setTextCursor(cursor)
_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "line one\nline two", "Up replaced typed text")
_check(box.textCursor().blockNumber() == 0, "Up on a typed box did not move the cursor")
_key(box, Qt.Key.Key_Down)
_check(box.textCursor().blockNumber() == 1, "Down on a typed box did not move the cursor")
print("CURSOR-ARROWS-OK")

box.clear()
_open(dock, "B", ["only in B"])
_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "only in B", f"case B recalled {box.toPlainText()!r}")
_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "only in B", "case B walked into case A's history")
box.clear()
_open(dock, "A", ["first in A", "!run build_mesh resolution=50"])
_key(box, Qt.Key.Key_Up)
_check(box.toPlainText() == "!run build_mesh resolution=50", "case A history not restored")
print("PER-CASE-OK")
