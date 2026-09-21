"""Shared Qt chrome and stylesheet constants used by more than one UI module."""

from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)


_STATUS_LINE_STYLE = "color: palette(mid); font-size: 8pt; padding-left: 4px;"


_THINKING_TOGGLE_STYLE = "color: palette(mid); font-size: 8pt; border: none; text-align: left;"
_THINKING_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid palette(mid); "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: palette(mid);"
)
# The probe-panel error variant: the same block chrome as the thinking body
# but in the error red, so a failed probe is unmistakable without landing in
# chat.
_PROBE_ERROR_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid #f85149; "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: #f85149;"
)


class CompactTitleBar(QWidget):
    """A slim ``setTitleBarWidget`` replacement: one fixed-height row with a
    small-font title, float and close -- the same two affordances the native
    title bar gives, without its generous padding and icon sizing."""

    _HEIGHT = 20  # px -- vs the platform-default title bar (30-40+ px)

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 2, 2)
        row.setSpacing(2)

        title = QLabel(dock.windowTitle())
        title.setStyleSheet("font-size: 8pt; font-weight: 600;")
        row.addWidget(title)
        row.addStretch(1)

        float_btn = QToolButton()
        float_btn.setText("⧉")  # float/dock glyph
        float_btn.setAutoRaise(True)
        float_btn.setFixedSize(16, 16)
        float_btn.setToolTip("Float/dock this window")
        float_btn.clicked.connect(
            lambda: dock.setFloating(not dock.isFloating())
        )
        row.addWidget(float_btn)

        close_btn = QToolButton()
        close_btn.setText("✕")  # close glyph
        close_btn.setAutoRaise(True)
        close_btn.setFixedSize(16, 16)
        close_btn.setToolTip("Close")
        close_btn.clicked.connect(dock.close)
        row.addWidget(close_btn)

        self.setFixedHeight(self._HEIGHT)
