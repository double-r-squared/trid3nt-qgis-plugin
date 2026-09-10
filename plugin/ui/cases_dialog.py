"""TRID3NT cases dialog."""
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
    """The user's cases, from the latest ``case-list`` envelope or the cold
    HTTP route when no connection exists yet. A LEFT CLICK opens a case, so
    rename and delete had to move to the context menu."""

    def __init__(self, dock: "Trid3ntDock", cases: List[CaseInfo]):
        super().__init__(dock)
        self._dock = dock
        self.setWindowTitle("TRID3NT cases")
        self.resize(460, 340)
        lay = QVBoxLayout(self)

        self.listw = QListWidget()
        # Rows are inline-EDITABLE for rename, but single-click-open fires
        # first, so it is the rename gesture that yields to open, by design.
        # ``_populating`` guards the ``itemChanged`` slot, so a programmatic
        # repopulation never mis-fires as a user rename.
        self._populating = False
        self.listw.itemClicked.connect(self._open_item)
        self.listw.itemDoubleClicked.connect(self._open_item)
        self.listw.itemChanged.connect(self._commit_rename)
        self.listw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
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
        """(Re)populate the list; safe to call live while the dialog is
        open."""
        selected = None
        current = self.listw.currentItem()
        if current is not None:
            selected = current.data(Qt.ItemDataRole.UserRole)
        # Guard the itemChanged rename slot while the list rebuilds, so a
        # programmatic clear and add never looks like a user rename commit.
        self._populating = True
        self.listw.clear()
        for case in cases:
            label = case.title
            if case.status and case.status != "active":
                label += f"  [{case.status}]"
            if case.updated_at:
                label += f"  ({case.updated_at[:10]})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, case.case_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, case.title)
            # Inline-editable for rename (F2 or the context menu).
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
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
        case_id = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        if isinstance(case_id, str) and case_id:
            # The cold-list open path rides the SAME action: ``open_case``
            # itself decides whether a direct select suffices or a
            # connect-then-queue is needed.
            self._dock.open_case(case_id, str(title))
            self.accept()

    def _show_context_menu(self, pos) -> None:
        item = self.listw.itemAt(pos)
        if item is None:
            return
        case_id = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        if not isinstance(case_id, str) or not case_id:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        global_pos = self.listw.viewport().mapToGlobal(pos)
        chosen = menu.exec(global_pos)
        if chosen is rename_action:
            self._begin_rename(item)
        elif chosen is delete_action:
            self._delete_case(case_id, str(title))

    def _begin_rename(self, item: QListWidgetItem) -> None:
        """Start the inline rename edit on ``item``, swapping the decorated
        row label for the PLAIN title first so the user edits just the name."""
        plain = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        self._populating = True
        item.setText(str(plain))
        self._populating = False
        self.listw.setCurrentItem(item)
        self.listw.editItem(item)

    def _commit_rename(self, item: QListWidgetItem) -> None:
        """An inline edit committed: send the rename, then refresh the list. A
        blank or unchanged title is a NO-OP that restores the row label."""
        if self._populating:
            return
        case_id = item.data(Qt.ItemDataRole.UserRole)
        old_title = item.data(Qt.ItemDataRole.UserRole + 1)
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
        item.setData(Qt.ItemDataRole.UserRole + 1, new_title)
        item.setText(new_title)
        self._populating = False
        self._dock.rename_case(case_id, new_title)
        self.info_lbl.setText(self._dock.refresh_cases())

    def _delete_case(self, case_id: str, title: str) -> None:
        reply = QMessageBox.question(
            self,
            "Delete case",
            f"Delete case '{title}'? This cannot be undone from the plugin.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._dock.delete_case(case_id, title)
