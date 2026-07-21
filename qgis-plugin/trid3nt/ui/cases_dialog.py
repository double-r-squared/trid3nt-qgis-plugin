"""TRID3NT cases dialog.

Split out of dock.py (2026-07-21 flat->package restructure). Behavior identical.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..net.trid3nt_client import CaseInfo
from ._style import _STATUS_LINE_STYLE

if TYPE_CHECKING:  # runtime-false: avoids a ui.dock <-> ui.cases_dialog import cycle
    from .dock import Trid3ntDock




class CasesDialog(QDialog):
    """The user's cases (latest ``case-list`` envelope, or -- before a WS
    connection exists -- the cold ``GET /api/case-list`` route; item b/c,
    live-feedback 2026-07-09):

      Refresh   debounced session-resume round trip.
      New case  case-command create -> dock rebind (fresh case-open).
      Click a case row (single click, or double-click) opens it and closes
                the dialog: ``Trid3ntDock.open_case`` -- case-command select
                -> dock rebind when already connected, or (item d) connects
                first and queues the select for the instant the handshake
                completes when the row came from the cold list.
      Right-click a case row -> context menu: Export GeoTIFFs / Delete
                (moved off the button row -- a left click now opens, so
                these secondary actions need a gesture that does not).
    """

    def __init__(self, dock: "Trid3ntDock", cases: List[CaseInfo]):
        super().__init__(dock)
        self._dock = dock
        self.setWindowTitle("TRID3NT cases")
        self.resize(460, 340)
        lay = QVBoxLayout(self)

        self.listw = QListWidget()
        # R1 (NATE 2026-07-20): single-click still OPENS a case (unchanged). The
        # rows are now inline-EDITABLE for rename (F2 via keyboard selection, or
        # the context-menu "Rename" which starts the same inline edit). Mouse
        # double-click keeps opening -- single-click-open fires first, so it is
        # the rename gesture that yields to open, by design ("do not let edit
        # mode hijack opening"). ``_populating`` guards the ``itemChanged`` slot
        # so programmatic repopulation (``set_cases``) never mis-fires a rename.
        self._populating = False
        self.listw.itemClicked.connect(self._open_item)
        self.listw.itemDoubleClicked.connect(self._open_item)
        self.listw.itemChanged.connect(self._commit_rename)
        self.listw.setContextMenuPolicy(Qt.CustomContextMenu)
        self.listw.customContextMenuRequested.connect(self._show_context_menu)
        lay.addWidget(self.listw, 1)

        self.info_lbl = QLabel("")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setStyleSheet(_STATUS_LINE_STYLE)
        lay.addWidget(self.info_lbl)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self.refresh_btn)
        self.new_btn = QPushButton("New case")
        self.new_btn.clicked.connect(self._new_case)
        row.addWidget(self.new_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.set_cases(cases)

    def set_cases(self, cases: List[CaseInfo]) -> None:
        """(Re)populate the list -- called live when a fresh ``case-list``
        lands while the dialog is open (the Refresh round trip, a New/
        Delete case-command reply, or the cold HTTP fetch landing)."""
        selected = None
        current = self.listw.currentItem()
        if current is not None:
            selected = current.data(Qt.UserRole)
        # R1: guard the itemChanged rename slot while we rebuild the list, so a
        # programmatic clear/add never looks like a user rename commit.
        self._populating = True
        self.listw.clear()
        for case in cases:
            label = case.title
            if case.status and case.status != "active":
                label += f"  [{case.status}]"
            if case.updated_at:
                label += f"  ({case.updated_at[:10]})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, case.case_id)
            item.setData(Qt.UserRole + 1, case.title)
            # R1: inline-editable for rename (double-click / F2 / context menu).
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self.listw.addItem(item)
            if case.case_id == selected:
                self.listw.setCurrentItem(item)
        self._populating = False
        if not cases:
            self.info_lbl.setText(
                "No cases received yet -- the list arrives from the agent on "
                "connect (case-list envelope). Try Refresh once connected, "
                "or start a New case."
            )
        elif self.info_lbl.text().startswith(("No cases", "Refreshing", "Loading")):
            self.info_lbl.setText("")

    def _refresh(self) -> None:
        self.info_lbl.setText(self._dock.refresh_cases())

    def _new_case(self) -> None:
        self._dock.new_case()
        self.accept()

    def _open_item(self, item: QListWidgetItem) -> None:
        case_id = item.data(Qt.UserRole)
        title = item.data(Qt.UserRole + 1) or item.text()
        if isinstance(case_id, str) and case_id:
            # Item d (live-feedback 2026-07-09): the cold-list open path
            # rides the SAME single-click action -- ``open_case`` itself
            # decides whether a direct select suffices or a connect-then-
            # queue is needed.
            self._dock.open_case(case_id, str(title))
            self.accept()

    def _show_context_menu(self, pos) -> None:
        item = self.listw.itemAt(pos)
        if item is None:
            return
        case_id = item.data(Qt.UserRole)
        title = item.data(Qt.UserRole + 1) or item.text()
        if not isinstance(case_id, str) or not case_id:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        export_action = menu.addAction("Export GeoTIFFs")
        delete_action = menu.addAction("Delete")
        global_pos = self.listw.viewport().mapToGlobal(pos)
        chosen = menu.exec_(global_pos) if hasattr(menu, "exec_") else menu.exec(global_pos)
        if chosen is rename_action:
            self._begin_rename(item)
        elif chosen is export_action:
            self._dock.open_case_in_qgis(case_id, str(title))
            self.accept()
        elif chosen is delete_action:
            self._delete_case(case_id, str(title))

    def _begin_rename(self, item: QListWidgetItem) -> None:
        """R1: start the inline rename edit on ``item``. The row text carries
        the decorated label (title + optional ``[status]`` + ``(date)``); swap
        it to the PLAIN title first so the user edits just the name, then open
        the editor. ``_populating`` guards this programmatic setText."""
        plain = item.data(Qt.UserRole + 1) or item.text()
        self._populating = True
        item.setText(str(plain))
        self._populating = False
        self.listw.setCurrentItem(item)
        self.listw.editItem(item)

    def _commit_rename(self, item: QListWidgetItem) -> None:
        """R1: an inline edit committed. Send the rename case-command (mirrors
        how delete/select flow through the dock bridge) with the case_id + new
        title, then refresh the list. A blank or unchanged title is a no-op that
        restores the row label. ``_populating`` short-circuits programmatic
        text changes (``set_cases`` / ``_begin_rename``)."""
        if self._populating:
            return
        case_id = item.data(Qt.UserRole)
        old_title = item.data(Qt.UserRole + 1)
        new_title = item.text().strip()
        if not isinstance(case_id, str) or not case_id:
            return
        if not new_title or new_title == old_title:
            # Restore the row's stored title (blank/no-op edits never rename).
            self._populating = True
            item.setText(str(old_title or ""))
            self._populating = False
            return
        # Optimistic local update (the refresh below re-authoritatively repaints
        # the decorated label once the server confirms).
        self._populating = True
        item.setData(Qt.UserRole + 1, new_title)
        item.setText(new_title)
        self._populating = False
        self._dock.rename_case(case_id, new_title)
        self.info_lbl.setText(self._dock.refresh_cases())

    def _delete_case(self, case_id: str, title: str) -> None:
        reply = QMessageBox.question(
            self,
            "Delete case",
            f"Delete case '{title}'? This cannot be undone from the plugin.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._dock.delete_case(case_id, title)
