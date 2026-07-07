"""TRID3NT chat dock -- message list, input, status dot, settings.

Milestone 1 chat surface (plain text bubbles):

  * user + assistant bubbles (assistant streams in via agent-message-chunk)
  * narration/pipeline-state frames render as small status lines under the
    pending assistant bubble (replace-not-reconcile, like the web)
  * layer materialization notes append as persistent status lines
  * connection status dot + settings dialog (mode local/remote, URLs, token)

All socket work lives on the AgentBridge worker thread; this widget only
handles Qt signals.
"""

from __future__ import annotations

import datetime
from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .layers import LayerMaterializer
from .plugin_settings import MODE_LOCAL, MODE_REMOTE, PluginSettings
from .trid3nt_client import PipelineStep
from .ws_bridge import AgentBridge

# LLM bookkeeping step names the web also hides from the tool timeline.
_LLM_STEP_NAMES = {
    "llm_generation", "gemini_generate", "thinking", "llm",
    "model_generate", "generate", "bedrock_generate", "ollama_generate",
}

_DOT_STYLE = "border-radius: 6px; min-width: 12px; max-width: 12px; min-height: 12px; max-height: 12px;"
_DOT_COLORS = {
    "disconnected": "#8b949e",
    "connecting": "#d29922",
    "connected": "#3fb950",
    "error": "#f85149",
}

_USER_BUBBLE_STYLE = (
    "background-color: #1f6feb; color: white; border-radius: 8px; padding: 6px 9px;"
)
_ASSISTANT_BUBBLE_STYLE = (
    "background-color: palette(midlight); border-radius: 8px; padding: 6px 9px;"
)
_STATUS_LINE_STYLE = "color: palette(mid); font-size: 8pt; padding-left: 4px;"
_ERROR_LINE_STYLE = "color: #f85149; font-size: 8pt; padding-left: 4px;"


class SettingsDialog(QDialog):
    """Mode local/remote, URLs, pasted token, MinIO endpoint."""

    def __init__(self, settings: PluginSettings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("TRID3NT settings")
        form = QFormLayout(self)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([MODE_LOCAL, MODE_REMOTE])
        self.mode_combo.setCurrentText(settings.mode)
        form.addRow("Mode", self.mode_combo)

        self.local_url_edit = QLineEdit(settings.local_url)
        form.addRow("Local agent URL", self.local_url_edit)

        self.remote_url_edit = QLineEdit(settings.remote_url)
        self.remote_url_edit.setPlaceholderText("wss://<host>/ws")
        form.addRow("Remote agent URL", self.remote_url_edit)

        self.token_edit = QLineEdit(settings.token)
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("paste bearer token (remote mode)")
        form.addRow("Remote token", self.token_edit)

        self.minio_edit = QLineEdit(settings.minio_endpoint)
        form.addRow("Local MinIO endpoint", self.minio_edit)

        note = QLabel(
            "Local mode connects anonymously. Remote mode sends the pasted "
            "token (?st= carrier + auth-token envelope). Reconnect to apply."
        )
        note.setWordWrap(True)
        note.setStyleSheet(_STATUS_LINE_STYLE)
        form.addRow(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        self._settings.mode = self.mode_combo.currentText()
        self._settings.local_url = self.local_url_edit.text()
        self._settings.remote_url = self.remote_url_edit.text()
        self._settings.token = self.token_edit.text()
        self._settings.minio_endpoint = self.minio_edit.text()
        super().accept()


class _AssistantEntry:
    """One pending/complete assistant bubble + its status-line area."""

    def __init__(self, parent_layout: QVBoxLayout):
        self.container = QWidget()
        lay = QVBoxLayout(self.container)
        lay.setContentsMargins(0, 2, 40, 2)
        lay.setSpacing(2)

        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setStyleSheet(_ASSISTANT_BUBBLE_STYLE)
        self.label.setVisible(False)  # only once text arrives
        lay.addWidget(self.label, 0, Qt.AlignLeft)

        # Transient pipeline lines (replaced on every pipeline-state frame).
        self.pipeline_area = QVBoxLayout()
        self.pipeline_area.setSpacing(0)
        lay.addLayout(self.pipeline_area)

        # Persistent notes (layer adds, errors) -- append-only.
        self.notes_area = QVBoxLayout()
        self.notes_area.setSpacing(0)
        lay.addLayout(self.notes_area)

        # Insert above the terminal stretch.
        parent_layout.insertWidget(parent_layout.count() - 1, self.container)
        self.text = ""

    def append_delta(self, delta: str) -> None:
        self.text += delta
        self.label.setText(self.text)
        self.label.setVisible(True)

    def set_pipeline_lines(self, lines: List[str]) -> None:
        while self.pipeline_area.count():
            item = self.pipeline_area.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for line in lines:
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.PlainText)
            lbl.setStyleSheet(_STATUS_LINE_STYLE)
            self.pipeline_area.addWidget(lbl)

    def add_note(self, text: str, error: bool = False) -> None:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setStyleSheet(_ERROR_LINE_STYLE if error else _STATUS_LINE_STYLE)
        self.notes_area.addWidget(lbl)


class Trid3ntDock(QDockWidget):
    """The chat dock widget."""

    def __init__(self, iface, parent: Optional[QWidget] = None):
        super().__init__("TRID3NT", parent)
        self.setObjectName("Trid3ntDock")
        self.iface = iface
        self.settings = PluginSettings()
        self.bridge = AgentBridge(self)
        self.materializer = LayerMaterializer(self.settings)
        self._pending: Optional[_AssistantEntry] = None
        self._connected = False
        self._case_id: Optional[str] = None

        self._build_ui()
        self._wire_bridge()

    # -- UI ---------------------------------------------------------------- #

    def _build_ui(self) -> None:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # Header: status dot | status text | settings | connect
        header = QHBoxLayout()
        self.dot = QLabel()
        self._set_dot("disconnected")
        header.addWidget(self.dot)
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("font-size: 9pt;")
        header.addWidget(self.status_label, 1)

        self.settings_btn = QToolButton()
        self.settings_btn.setText("Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        self.connect_btn = QToolButton()
        self.connect_btn.setText("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        header.addWidget(self.connect_btn)
        outer.addLayout(header)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        outer.addWidget(line)

        # Message list
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.messages_host = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_host)
        self.messages_layout.setContentsMargins(2, 2, 2, 2)
        self.messages_layout.setSpacing(4)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_host)
        outer.addWidget(self.scroll, 1)

        # Input row
        input_row = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Ask for data or a simulation...")
        self.input_edit.returnPressed.connect(self._send)
        input_row.addWidget(self.input_edit, 1)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        input_row.addWidget(self.send_btn)
        outer.addLayout(input_row)

        self.setWidget(body)

    def _set_dot(self, state: str) -> None:
        color = _DOT_COLORS.get(state, _DOT_COLORS["disconnected"])
        self.dot.setStyleSheet(f"background-color: {color}; {_DOT_STYLE}")

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add_user_bubble(self, text: str) -> None:
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(40, 2, 0, 2)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet(_USER_BUBBLE_STYLE)
        lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        lay.addWidget(lbl, 0, Qt.AlignRight)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, container)
        self._scroll_to_bottom()

    def _ensure_pending(self) -> _AssistantEntry:
        if self._pending is None:
            self._pending = _AssistantEntry(self.messages_layout)
        return self._pending

    # -- connection ----------------------------------------------------------- #

    def _wire_bridge(self) -> None:
        self.bridge.connected.connect(self._on_connected)
        self.bridge.case_ready.connect(self._on_case_ready)
        self.bridge.event.connect(self._on_event)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.closed.connect(self._on_closed)

    def connect_agent(self) -> None:
        if self.bridge.running:
            return
        url = self.settings.effective_url()
        if not url or url == "wss://":
            self.status_label.setText("Set the agent URL in Settings first")
            self._set_dot("error")
            return
        self._set_dot("connecting")
        self.status_label.setText(f"Connecting to {url} ...")
        self.connect_btn.setText("Disconnect")
        title = "QGIS session " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        anon = self.settings.anonymous_user_id or None
        self.bridge.start(
            url,
            token=self.settings.effective_token(),
            anonymous_user_id=anon if self.settings.mode == MODE_LOCAL else None,
            case_title=title,
        )

    def disconnect_agent(self) -> None:
        self.bridge.stop()
        self._connected = False
        self._case_id = None
        self._set_dot("disconnected")
        self.status_label.setText("Not connected")
        self.connect_btn.setText("Connect")

    def _toggle_connection(self) -> None:
        if self.bridge.running:
            self.disconnect_agent()
        else:
            self.connect_agent()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()

    # -- bridge slots (UI thread) ---------------------------------------------- #

    def _on_connected(self, user_id: str, is_anonymous: bool) -> None:
        self._connected = True
        if is_anonymous and self.settings.mode == MODE_LOCAL:
            self.settings.anonymous_user_id = user_id
        self.status_label.setText(f"Connected ({'anonymous' if is_anonymous else user_id})")

    def _on_case_ready(self, case_id: str) -> None:
        self._case_id = case_id
        self.materializer.set_case(case_id)
        self._set_dot("connected")
        self.status_label.setText(f"Connected -- case {case_id[:8]}")

    def _on_failed(self, message: str) -> None:
        self._connected = False
        self._set_dot("error")
        self.status_label.setText(f"Connection failed: {message}")
        self.connect_btn.setText("Connect")

    def _on_closed(self, reason: str) -> None:
        self._connected = False
        self._case_id = None
        if reason != "stopped":
            self._set_dot("error")
            self.status_label.setText(f"Disconnected: {reason}")
        self.connect_btn.setText("Connect")

    def _on_event(self, kind: str, data: object) -> None:
        if not isinstance(data, dict):
            return
        if kind == "chunk":
            entry = self._ensure_pending()
            entry.append_delta(str(data.get("delta") or ""))
            self._scroll_to_bottom()
        elif kind == "pipeline":
            steps = data.get("steps") or []
            lines = []
            for step in steps:
                if not isinstance(step, PipelineStep):
                    continue
                if step.tool_name.lower() in _LLM_STEP_NAMES:
                    continue
                if step.parent_step_id:
                    lines.append(f"    {step.tool_name} - {step.state}")
                else:
                    label = step.tool_name or step.name
                    suffix = f" ({step.substep_label})" if step.substep_label else ""
                    lines.append(f"{label} - {step.state}{suffix}")
                    if step.error_message:
                        lines.append(f"    {step.error_message}")
            self._ensure_pending().set_pipeline_lines(lines)
            self._scroll_to_bottom()
        elif kind == "session-state":
            layers = data.get("layers") or []
            if layers and self._case_id:
                notes = self.materializer.materialize(layers)
                if notes:
                    entry = self._ensure_pending()
                    for note in notes:
                        entry.add_note(note)
                    self._scroll_to_bottom()
        elif kind == "error":
            code = data.get("error_code") or "ERROR"
            message = data.get("message") or data.get("detail") or ""
            self._ensure_pending().add_note(f"{code}: {message}", error=True)
            self._scroll_to_bottom()
        elif kind == "payload-warning":
            # Milestone 2 renders the real gate card; surface honestly for now.
            self._ensure_pending().add_note(
                "Tool payload warning received -- the sim gate card lands in "
                "milestone 2; use the web app to confirm this run.",
            )
            self._scroll_to_bottom()
        elif kind == "turn-complete":
            if self._pending is not None:
                self._pending = None

    # -- sending ------------------------------------------------------------- #

    def _send(self) -> None:
        text = self.input_edit.text().strip()
        if not text:
            return
        if not (self._connected and self._case_id and self.bridge.running):
            self.status_label.setText("Not connected -- press Connect first")
            return
        self.input_edit.clear()
        self._add_user_bubble(text)
        self._pending = _AssistantEntry(self.messages_layout)
        self._scroll_to_bottom()
        try:
            self.bridge.send_chat(text)
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"send failed: {exc}", error=True)

    # -- teardown ------------------------------------------------------------- #

    def shutdown(self) -> None:
        self.bridge.stop()
