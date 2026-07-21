"""TRID3NT QGIS plugin -- registers the chat dock + toolbar action."""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

_ICON_PATH = os.path.join(os.path.dirname(__file__), "icon.svg")


class Trid3ntPlugin:
    """Plugin lifecycle: toolbar icon + menu entry toggling the chat dock."""

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None

    # -- QGIS plugin API ------------------------------------------------------ #

    def initGui(self) -> None:  # noqa: N802 -- QGIS-mandated name
        self.action = QAction(QIcon(_ICON_PATH), "TRID3NT chat", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("TRID3NT", self.action)

    def unload(self) -> None:
        if self.dock is not None:
            self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("TRID3NT", self.action)
            self.action.deleteLater()
            self.action = None

    # -- behavior -------------------------------------------------------------- #

    def toggle_dock(self, checked: bool) -> None:
        if self.dock is None:
            # Lazy import so plugin discovery stays cheap and a Qt problem in
            # the dock cannot break classFactory.
            from .ui.dock import Trid3ntDock

            self.dock = Trid3ntDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self._sync_action)
        self.dock.setVisible(checked)

    def _sync_action(self, visible: bool) -> None:
        if self.action is not None:
            self.action.setChecked(visible)
