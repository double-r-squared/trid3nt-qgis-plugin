"""The Library window -- a bottom-docked, read-only listing of what can be run.

The left tree carries the subsystems and the data classes, the middle list the
selected group's entries, the right pane the selected entry's description, the
exact name a direct run takes and its facts. The search box runs the daemon's
BM25 route -- the same corpus the model routes on. This window OWNS no network
call and no case state: the dock fetches and hands results in."""

from __future__ import annotations

import html
from typing import List, Optional

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ._style import _STATUS_LINE_STYLE, CompactTitleBar
from ..net.trid3nt_client import Library, LibraryItem

_TREE_WIDTH = 200  # px -- the group strip
_LIST_WIDTH = 240  # px -- the entry strip
_NAME_STYLE = (
    "font-family: monospace; font-size: 9pt; padding: 3px; "
    "background: rgba(127,127,127,0.15); border-radius: 3px;"
)

#: Tree roles: the items a tree node shows, and the node's own title.
_ITEMS_ROLE = Qt.ItemDataRole.UserRole
_TITLE_ROLE = Qt.ItemDataRole.UserRole + 1


class LibraryWindow(QDockWidget):
    """The library panel. ``searchRequested`` carries a non-empty query;
    ``refreshRequested`` asks the dock to re-fetch the listing."""

    searchRequested = pyqtSignal(str)
    refreshRequested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("TRID3NT Library", parent)
        self.setObjectName("Trid3ntLibraryWindow")
        self.setTitleBarWidget(CompactTitleBar(self))
        self._items: List[LibraryItem] = []
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self) -> None:
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Search tools and data rows (same index the assistant routes on)"
        )
        self.search_edit.returnPressed.connect(self._on_search)
        search_row.addWidget(self.search_edit, 1)
        self.search_btn = QToolButton()
        self.search_btn.setText("Search")
        self.search_btn.clicked.connect(self._on_search)
        search_row.addWidget(self.search_btn)
        self.browse_btn = QToolButton()
        self.browse_btn.setText("Browse")
        self.browse_btn.setToolTip("Leave the search hits and browse the listing")
        self.browse_btn.clicked.connect(self._on_browse)
        search_row.addWidget(self.browse_btn)
        root.addLayout(search_row)

        panes = QHBoxLayout()
        panes.setContentsMargins(0, 0, 0, 0)
        panes.setSpacing(6)

        self.group_tree = QTreeWidget()
        self.group_tree.setFixedWidth(_TREE_WIDTH)
        self.group_tree.setHeaderHidden(True)
        self.group_tree.currentItemChanged.connect(self._on_group)
        panes.addWidget(self.group_tree)

        self.entry_list = QListWidget()
        self.entry_list.setFixedWidth(_LIST_WIDTH)
        self.entry_list.currentRowChanged.connect(self._on_entry)
        panes.addWidget(self.entry_list)

        detail = QVBoxLayout()
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(4)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        self.name_label = QLabel("")
        self.name_label.setStyleSheet(_NAME_STYLE)
        self.name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        name_row.addWidget(self.name_label, 1)
        self.copy_btn = QToolButton()
        self.copy_btn.setText("Copy name")
        self.copy_btn.setToolTip("Copy this name for a direct !run")
        self.copy_btn.clicked.connect(self._on_copy)
        self.copy_btn.setEnabled(False)
        name_row.addWidget(self.copy_btn)
        detail.addLayout(name_row)
        self.description_label = QLabel("")
        self.description_label.setWordWrap(True)
        self.description_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.description_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        detail.addWidget(self.description_label, 1)
        self.facts_label = QLabel("")
        self.facts_label.setWordWrap(True)
        self.facts_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail.addWidget(self.facts_label)
        panes.addLayout(detail, 1)
        root.addLayout(panes, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(_STATUS_LINE_STYLE)
        root.addWidget(self.status_label)

        self.setWidget(body)

    @property
    def shown_items(self) -> List[LibraryItem]:
        """The entries the middle list currently offers."""
        return list(self._items)

    def set_library(self, library: Library) -> None:
        """Repaint the tree from a listing and select its first group. Replaces
        whatever was shown, search hits included."""
        self.group_tree.clear()
        tools_root = QTreeWidgetItem(["Tools"])
        tools_root.setData(0, _ITEMS_ROLE, None)
        for name, items in library.subsystems:
            node = QTreeWidgetItem([f"{name} ({len(items)})"])
            node.setData(0, _ITEMS_ROLE, items)
            node.setData(0, _TITLE_ROLE, name)
            tools_root.addChild(node)
        data_root = QTreeWidgetItem(["Data"])
        data_root.setData(0, _ITEMS_ROLE, None)
        for class_name, kinds in library.classes:
            class_node = QTreeWidgetItem([class_name])
            class_items: List[LibraryItem] = []
            for kind_name, rows in kinds:
                kind_node = QTreeWidgetItem([f"{kind_name} ({len(rows)})"])
                kind_node.setData(0, _ITEMS_ROLE, rows)
                kind_node.setData(0, _TITLE_ROLE, f"{class_name} / {kind_name}")
                class_node.addChild(kind_node)
                class_items.extend(rows)
            class_node.setData(0, _ITEMS_ROLE, class_items)
            class_node.setData(0, _TITLE_ROLE, class_name)
            data_root.addChild(class_node)
        for root_node in (tools_root, data_root):
            self.group_tree.addTopLevelItem(root_node)
            root_node.setExpanded(True)
        first = tools_root.child(0) or data_root.child(0)
        if first is not None:
            self.group_tree.setCurrentItem(first)
        else:
            self._show_items([], "")
        counts = (
            f"{sum(len(i) for _, i in library.subsystems)} tools, "
            f"{len(library.classes)} data classes"
        )
        self.status_label.setText(counts)

    def set_hits(self, query: str, hits: List[LibraryItem]) -> None:
        """Show ranked search hits in place of a group's entries. The tree keeps
        its selection so Browse returns to it."""
        self._show_items(hits, f"search: {query}")
        self.status_label.setText(
            f"{len(hits)} hit(s) for {query!r}" if hits
            else f"no hit for {query!r}"
        )

    def set_status(self, text: str) -> None:
        """The one status line: counts, or an honest failure message."""
        self.status_label.setText(text)

    def _show_items(self, items: List[LibraryItem], title: str) -> None:
        self._items = list(items)
        self.entry_list.blockSignals(True)
        self.entry_list.clear()
        for item in self._items:
            row = QListWidgetItem(item.name)
            row.setToolTip(item.group or title)
            self.entry_list.addItem(row)
        self.entry_list.blockSignals(False)
        if self._items:
            self.entry_list.setCurrentRow(0)
        else:
            self._paint_detail(None)

    def _on_group(self, current, _previous=None) -> None:
        if current is None:
            return
        items = current.data(0, _ITEMS_ROLE)
        if items is None:
            return
        self._show_items(items, current.data(0, _TITLE_ROLE) or "")

    def _on_entry(self, row: int) -> None:
        if 0 <= row < len(self._items):
            self._paint_detail(self._items[row])
        else:
            self._paint_detail(None)

    def _paint_detail(self, item: Optional[LibraryItem]) -> None:
        if item is None:
            self.name_label.setText("")
            self.description_label.setText("")
            self.facts_label.setText("")
            self.copy_btn.setEnabled(False)
            return
        self.name_label.setText(item.name)
        self.description_label.setText(item.description or "(no description)")
        facts = "<br>".join(
            f"<b>{html.escape(label)}</b>: {html.escape(value)}"
            for label, value in item.facts
        )
        self.facts_label.setText(facts)
        self.copy_btn.setEnabled(True)

    def _on_copy(self) -> None:
        row = self.entry_list.currentRow()
        if not (0 <= row < len(self._items)):
            return
        name = self._items[row].name
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(name)
        self.status_label.setText(f"Copied {name} -- type it after !run")

    def _on_search(self) -> None:
        query = self.search_edit.text().strip()
        if not query:
            self._on_browse()
            return
        self.status_label.setText(f"searching for {query!r} ...")
        self.searchRequested.emit(query)

    def _on_browse(self) -> None:
        current = self.group_tree.currentItem()
        if current is None:
            self.refreshRequested.emit()
            return
        self._on_group(current)
