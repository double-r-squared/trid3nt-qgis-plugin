"""TRID3NT chat dock -- message list, input, status dot, settings.

Milestone 3 additions on top of milestone 2:

  * REMOTE Open-in-QGIS: mode=remote POSTs /api/export-qgis on the HTTP base
    derived from the remote WS URL, downloads the .gpkg/.qgz artifacts through
    GET /api/export-qgis/file into a local temp dir, then adds layers exactly
    like local mode (rasters become an honest skipped note -- the file route
    serves only .qgz/.gpkg).
  * CASE SWITCHING: the Cases dialog gains "Open chat" -> case-command select;
    the server's case-open rehydration rebinds the dock (header case title,
    fresh layer group, replayed layers).
  * CASE-LIST REFRESH: no list-cases verb exists; refresh = one debounced
    session-resume round trip (the web's own keepalive -- documented tradeoff
    in trid3nt_client.request_case_list_refresh).
  * SELECTED-POLYGON AOI: opt-in toggle; the active layer's selection bbox
    (v1: bbox, not the exact ring) overrides the canvas extent on case-create
    and the per-message context line.
  * TOKEN UX: Settings explains where a token comes from (?st= carrier); an
    auth-classified failure STOPS the reconnect ladder and paints an honest
    "token expired -- paste a fresh one" status instead of silently looping.

Milestone 2 chat surface on top of milestone 1's plain-text bubbles:

  * GATE CARD: ``tool-payload-warning`` envelopes render as an inline Qt card
    (title, the agent's honest numbers/recommendation, the #154 resolution
    ladder when the envelope carries one, editable cadence/window when a
    time_scale rides along) with Proceed / Cancel wired to
    ``tool-payload-confirmation``. Sims never start without a click here
    (user-controlled granularity, standing directive). Decision rules live in
    the pure ``gate`` module (tested without Qt).
  * CANVAS AOI: "Use map canvas as area of interest" toggle (default ON) --
    the case is created with the canvas extent as ``args.bbox`` (#170
    AOI-first) and every outgoing message carries the CURRENT extent as an
    in-text context line (see ``aoi`` module docstring for why the wire
    contract forbids a per-message field). >2 deg/side extents are honestly
    dropped with a status note.
  * RECONNECT: the bridge's capped-jitter ladder drives the status dot
    (connecting amber / connected green / lost red); queued sends flush on
    resume.
  * OPEN CASE IN QGIS: a Cases dialog listing the ``case-list`` envelope,
    with per-case "Open in QGIS" -> POST /api/export-qgis on the local agent,
    then the exported GeoPackage tables + GeoTIFFs are ADDED to the current
    project (never ``QgsProject.read()`` -- that would replace the user's
    open project; rationale in ``case_export``).

All socket work lives on the AgentBridge worker thread; this widget only
handles Qt signals. The export POST runs on a plain worker thread emitting
cross-thread signals (auto-queued by Qt).
"""

from __future__ import annotations

import datetime
import tempfile
import threading
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import aoi, case_export, gate
from .layers import LayerMaterializer
from .plugin_settings import MODE_LOCAL, MODE_REMOTE, PluginSettings
from .trid3nt_client import CaseInfo, Debouncer, PipelineStep, parse_case_open
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
# Amber caution frame for the gate card (mirrors the web's warning palette).
_GATE_CARD_STYLE = (
    "QFrame { border: 1px solid #d29922; border-radius: 8px; }"
)
_GATE_TITLE_STYLE = "color: #d29922; font-weight: bold; border: none;"
_GATE_BODY_STYLE = "border: none; font-size: 9pt;"
_GATE_NOTE_STYLE = "border: none; color: palette(mid); font-size: 8pt;"


class SettingsDialog(QDialog):
    """Mode local/remote, URLs, pasted token, MinIO + export API endpoints."""

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

        # Milestone 3 item 5: token HELP, not a Cognito flow. Honest about
        # where a token comes from today and what expiry looks like.
        token_help = QLabel(
            "Get a token: sign in to the TRID3NT web app, open the browser "
            "dev tools (F12) > Network > WS, and copy the value of the "
            "st= query parameter on the /ws WebSocket request. That is the "
            "carrier the cloud broker authenticates BEFORE the upgrade; the "
            "plugin sends it the same way (plus the in-band auth-token "
            "envelope). Tokens EXPIRE: when the dock status says the token "
            "expired, paste a fresh one here and press Connect -- the plugin "
            "will not silently retry a dead token."
        )
        token_help.setWordWrap(True)
        token_help.setStyleSheet(_STATUS_LINE_STYLE)
        form.addRow("Get token help", token_help)

        self.minio_edit = QLineEdit(settings.minio_endpoint)
        form.addRow("Local MinIO endpoint", self.minio_edit)

        self.export_api_edit = QLineEdit(settings.export_api)
        form.addRow("Local export API", self.export_api_edit)

        note = QLabel(
            "Local mode connects anonymously. Remote mode sends the pasted "
            "token (?st= carrier + auth-token envelope). The export API is "
            "the local agent's HTTP listener (Open case in QGIS). "
            "Reconnect to apply."
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
        self._settings.export_api = self.export_api_edit.text()
        super().accept()


# Style constants for the thinking block (F9, live-feedback 2026-07-09).
_THINKING_TOGGLE_STYLE = "color: palette(mid); font-size: 8pt; border: none; text-align: left;"
_THINKING_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid palette(mid); "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: palette(mid);"
)


class _AssistantEntry:
    """One pending/complete assistant bubble + its status-line area."""

    def __init__(self, parent_layout: QVBoxLayout):
        self.container = QWidget()
        lay = QVBoxLayout(self.container)
        lay.setContentsMargins(0, 2, 40, 2)
        lay.setSpacing(2)

        # F9 thinking block: toggle button + collapsible text label.
        # Hidden until the first thinking-chunk arrives.
        self._thinking_container = QWidget()
        thinking_lay = QVBoxLayout(self._thinking_container)
        thinking_lay.setContentsMargins(0, 0, 0, 0)
        thinking_lay.setSpacing(0)

        self._thinking_toggle = QPushButton("Thinking...")
        self._thinking_toggle.setFlat(True)
        self._thinking_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self._thinking_toggle.setCheckable(True)
        self._thinking_toggle.setChecked(True)  # expanded while streaming
        self._thinking_toggle.clicked.connect(self._toggle_thinking)
        thinking_lay.addWidget(self._thinking_toggle)

        self._thinking_label = QLabel("")
        self._thinking_label.setWordWrap(True)
        self._thinking_label.setTextFormat(Qt.PlainText)
        self._thinking_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._thinking_label.setStyleSheet(_THINKING_BLOCK_STYLE)
        thinking_lay.addWidget(self._thinking_label)

        self._thinking_container.setVisible(False)
        lay.addWidget(self._thinking_container)
        self._thinking_text = ""

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

    # -- thinking block ---------------------------------------------------- #

    def _toggle_thinking(self) -> None:
        self._thinking_label.setVisible(self._thinking_toggle.isChecked())

    def append_thinking_delta(self, delta: str) -> None:
        """Accumulate a reasoning-channel token delta; show the thinking block."""
        self._thinking_text += delta
        self._thinking_label.setText(self._thinking_text)
        self._thinking_container.setVisible(True)

    def collapse_thinking(self) -> None:
        """Collapse the thinking block once the answer starts streaming."""
        self._thinking_toggle.setChecked(False)
        self._thinking_toggle.setText("Thought process")
        self._thinking_label.setVisible(False)

    # -- answer text ------------------------------------------------------- #

    def append_delta(self, delta: str) -> None:
        if not self.text and self._thinking_text:
            # First answer token: collapse the thinking block.
            self.collapse_thinking()
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


class GateCard(QFrame):
    """Inline confirmation card for one ``tool-payload-warning``.

    Renders the honest envelope numbers (tool, estimated vs threshold MB,
    recommendation), the #154 granularity ladder when present (rung combo +
    live cells/ETA recompute via the mirrored web math), editable cadence /
    window when a ``time_scale`` rides along, and Proceed / Cancel. Decision
    mapping (proceed / narrow_scope+revised_args / cancel) is delegated to
    ``gate.resolve_gate_decision`` -- the exact web ResolutionPickerCard
    rules. Once answered the card locks (no re-answer) and folds to a
    one-line summary.
    """

    def __init__(self, warning: gate.PayloadWarning, on_decide, parent=None):
        super().__init__(parent)
        self._warning = warning
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        self.setStyleSheet(_GATE_CARD_STYLE)
        self.setFrameShape(QFrame.StyledPanel)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)

        title = "Confirm run settings" if warning.granularity else "Large response expected"
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(_GATE_TITLE_STYLE)
        lay.addWidget(title_lbl)

        for line in gate.summary_lines(warning):
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.PlainText)
            lbl.setStyleSheet(_GATE_BODY_STYLE)
            lay.addWidget(lbl)

        # -- resolution ladder ------------------------------------------------ #
        self.res_combo: Optional[QComboBox] = None
        self.res_estimate_lbl: Optional[QLabel] = None
        rungs = warning.resolution_choices
        suggested = warning.suggested_resolution_m
        if warning.granularity and suggested is not None:
            if suggested not in rungs:
                rungs = sorted(set(rungs) | {suggested})
            row = QHBoxLayout()
            row.addWidget(self._plain_label("Resolution:"))
            self.res_combo = QComboBox()
            for rung in rungs:
                label = f"{rung:g} m" + (" (suggested)" if rung == suggested else "")
                self.res_combo.addItem(label, rung)
            self.res_combo.setCurrentIndex(rungs.index(suggested))
            self.res_combo.currentIndexChanged.connect(self._refresh_estimates)
            row.addWidget(self.res_combo)
            self.res_estimate_lbl = QLabel("")
            self.res_estimate_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            row.addWidget(self.res_estimate_lbl, 1)
            lay.addLayout(row)

        # -- time scale (cadence + window) ------------------------------------ #
        self.interval_edit: Optional[QLineEdit] = None
        self.duration_edit: Optional[QLineEdit] = None
        self.frames_lbl: Optional[QLabel] = None
        ts = warning.time_scale
        if ts:
            row = QHBoxLayout()
            row.addWidget(self._plain_label("Frame every"))
            self.interval_edit = QLineEdit(f"{ts.get('suggested_interval_min') or 0:g}")
            self.interval_edit.setMaximumWidth(56)
            self.interval_edit.textChanged.connect(self._refresh_estimates)
            row.addWidget(self.interval_edit)
            row.addWidget(self._plain_label("min over"))
            self.duration_edit = QLineEdit(f"{ts.get('suggested_duration_hr') or 0:g}")
            self.duration_edit.setMaximumWidth(56)
            self.duration_edit.textChanged.connect(self._refresh_estimates)
            row.addWidget(self.duration_edit)
            row.addWidget(self._plain_label("h"))
            self.frames_lbl = QLabel("")
            self.frames_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            row.addWidget(self.frames_lbl, 1)
            lay.addLayout(row)

        # -- buttons ----------------------------------------------------------- #
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.proceed_btn = QPushButton("Proceed")
        self.proceed_btn.clicked.connect(self._proceed)
        btn_row.addWidget(self.proceed_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

        self._refresh_estimates()

    @staticmethod
    def _plain_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(_GATE_BODY_STYLE)
        return lbl

    # -- UI state ------------------------------------------------------------- #

    def _chosen_resolution(self) -> Optional[float]:
        if self.res_combo is None:
            return None
        value = self.res_combo.currentData()
        return float(value) if isinstance(value, (int, float)) else None

    def _edited_float(self, edit: Optional[QLineEdit]) -> Optional[float]:
        if edit is None:
            return None
        try:
            value = float(edit.text())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _refresh_estimates(self, *_args) -> None:
        w = self._warning
        chosen = self._chosen_resolution()
        if w.granularity and chosen and self.res_estimate_lbl is not None:
            cells = gate.estimate_cells(w.granularity, chosen)
            eta = gate.estimate_eta_seconds(w.granularity, chosen)
            self.res_estimate_lbl.setText(f"~{cells:,} cells, est ~{eta:.0f}s")
        if w.time_scale and self.frames_lbl is not None:
            interval = self._edited_float(self.interval_edit) or 0.0
            duration = self._edited_float(self.duration_edit) or 0.0
            frames = gate.estimate_frames(w.time_scale, interval, duration)
            self.frames_lbl.setText(f"~{frames} frames")
        # Hard cap: Proceed stays enabled only when the current choice maps
        # to a decision the envelope's options actually allow.
        if self._decided is None:
            decision = self._current_decision()
            self.proceed_btn.setEnabled(decision.decision is not None)
            self.proceed_btn.setToolTip(decision.note or "")

    def _current_decision(self) -> gate.GateDecision:
        return gate.resolve_gate_decision(
            self._warning,
            cancel=False,
            chosen_resolution_m=self._chosen_resolution(),
            interval_min=self._edited_float(self.interval_edit),
            duration_hr=self._edited_float(self.duration_edit),
        )

    # -- actions --------------------------------------------------------------- #

    def _proceed(self) -> None:
        decision = self._current_decision()
        if decision.decision is None:
            self.result_lbl.setText(decision.note)
            self.result_lbl.setVisible(True)
            return
        self._commit(decision)

    def _cancel(self) -> None:
        self._commit(gate.resolve_gate_decision(self._warning, cancel=True))

    def _commit(self, decision: gate.GateDecision) -> None:
        if self._decided is not None:
            return  # locked -- a gate is answered exactly once
        self._decided = decision.decision
        self._on_decide(self._warning.warning_id, decision.decision, decision.revised_args)
        for widget in (self.proceed_btn, self.cancel_btn, self.res_combo,
                       self.interval_edit, self.duration_edit):
            if widget is not None:
                widget.setEnabled(False)
        summary = {
            "proceed": "Confirmed -- proceeding.",
            "cancel": "Cancelled.",
            "narrow_scope": f"Confirmed with overrides: {decision.revised_args}",
        }.get(decision.decision or "", "")
        self.result_lbl.setText(summary)
        self.result_lbl.setVisible(True)


class _ExportTask(QObject):
    """POST /api/export-qgis off the UI thread (cross-thread signal emit).

    ``remote=True`` (milestone 3 item 1) additionally downloads the produced
    .gpkg/.qgz through GET /api/export-qgis/file into a fresh local temp dir
    and rewrites the result's paths to the local copies, so the finished
    slot can plan layers exactly like local mode.
    """

    finished = pyqtSignal(str, dict)  # case_id, result (localized if remote)
    errored = pyqtSignal(str, str)    # case_id, message

    def __init__(
        self,
        base_url: str,
        case_id: str,
        parent: Optional[QObject] = None,
        remote: bool = False,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._case_id = case_id
        self._remote = remote

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = case_export.post_export_case(self._base_url, self._case_id)
            if self._remote:
                dest_dir = tempfile.mkdtemp(prefix="trid3nt_remote_export_")
                result = case_export.localize_remote_export(
                    self._base_url, result, dest_dir
                )
        except case_export.ExportRequestError as exc:
            self.errored.emit(self._case_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(self._case_id, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(self._case_id, result)


class CasesDialog(QDialog):
    """The user's cases (latest ``case-list`` envelope): Refresh (debounced
    resume round trip), Open chat (case-command select -> dock rebind), and
    Open in QGIS (export API, local or remote)."""

    def __init__(self, dock: "Trid3ntDock", cases: List[CaseInfo]):
        super().__init__(dock)
        self._dock = dock
        self.setWindowTitle("TRID3NT cases")
        self.resize(460, 340)
        lay = QVBoxLayout(self)

        self.listw = QListWidget()
        lay.addWidget(self.listw, 1)

        self.info_lbl = QLabel("")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setStyleSheet(_STATUS_LINE_STYLE)
        lay.addWidget(self.info_lbl)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        self.chat_btn = QPushButton("Open chat")
        self.chat_btn.clicked.connect(self._open_chat_selected)
        row.addWidget(self.chat_btn)
        self.open_btn = QPushButton("Open in QGIS")
        self.open_btn.clicked.connect(self._open_selected)
        row.addWidget(self.open_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        row.addWidget(close_btn)
        lay.addLayout(row)

        self.set_cases(cases)

    def set_cases(self, cases: List[CaseInfo]) -> None:
        """(Re)populate the list -- called live when a fresh ``case-list``
        lands while the dialog is open (the Refresh round trip)."""
        selected = None
        current = self.listw.currentItem()
        if current is not None:
            selected = current.data(Qt.UserRole)
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
            self.listw.addItem(item)
            if case.case_id == selected:
                self.listw.setCurrentItem(item)
        self.open_btn.setEnabled(bool(cases))
        self.chat_btn.setEnabled(bool(cases))
        if not cases:
            self.info_lbl.setText(
                "No cases received yet -- the list arrives from the agent on "
                "connect (case-list envelope). Try Refresh once connected."
            )
        elif self.info_lbl.text().startswith(("No cases", "Refreshing")):
            self.info_lbl.setText("")

    def _refresh(self) -> None:
        self.info_lbl.setText(self._dock.refresh_cases())

    def _selected(self) -> Tuple[Optional[str], str]:
        item = self.listw.currentItem()
        if item is None:
            return None, ""
        case_id = item.data(Qt.UserRole)
        title = item.data(Qt.UserRole + 1) or item.text()
        if isinstance(case_id, str) and case_id:
            return case_id, str(title)
        return None, ""

    def _open_chat_selected(self) -> None:
        case_id, title = self._selected()
        if case_id:
            self._dock.select_case(case_id, title)
            self.accept()

    def _open_selected(self) -> None:
        case_id, title = self._selected()
        if case_id:
            self._dock.open_case_in_qgis(case_id, title)
            self.accept()


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
        self._case_title: str = ""
        self._session_case_title: str = ""
        self._cases: List[CaseInfo] = []
        self._cases_dialog: Optional[CasesDialog] = None
        self._export_tasks: List[_ExportTask] = []  # keep-alive refs
        self._refresh_debounce = Debouncer()

        self._build_ui()
        self._wire_bridge()

    # -- UI ---------------------------------------------------------------- #

    def _build_ui(self) -> None:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # Header: status dot | status text | cases | settings | connect
        header = QHBoxLayout()
        self.dot = QLabel()
        self._set_dot("disconnected")
        header.addWidget(self.dot)
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("font-size: 9pt;")
        header.addWidget(self.status_label, 1)

        self.cases_btn = QToolButton()
        self.cases_btn.setText("Cases")
        self.cases_btn.clicked.connect(self._open_cases)
        header.addWidget(self.cases_btn)

        self.settings_btn = QToolButton()
        self.settings_btn.setText("Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        self.connect_btn = QToolButton()
        self.connect_btn.setText("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        header.addWidget(self.connect_btn)
        outer.addLayout(header)

        # Active-case title (milestone 3 case switching): which case the
        # chat and the layer group are bound to right now.
        self.case_label = QLabel("")
        self.case_label.setStyleSheet("font-size: 9pt; font-weight: bold;")
        self.case_label.setWordWrap(True)
        self.case_label.setVisible(False)
        outer.addWidget(self.case_label)

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

        # AOI rows: canvas toggle + selection override + status line
        aoi_row = QHBoxLayout()
        self.aoi_checkbox = QCheckBox("Use map canvas as area of interest")
        self.aoi_checkbox.setChecked(self.settings.canvas_aoi)
        self.aoi_checkbox.toggled.connect(self._on_aoi_toggled)
        aoi_row.addWidget(self.aoi_checkbox)
        outer.addLayout(aoi_row)
        sel_row = QHBoxLayout()
        self.selection_checkbox = QCheckBox(
            "Use selected polygon as AOI (overrides canvas)"
        )
        self.selection_checkbox.setChecked(self.settings.selection_aoi)
        self.selection_checkbox.toggled.connect(self._on_selection_aoi_toggled)
        sel_row.addWidget(self.selection_checkbox)
        outer.addLayout(sel_row)
        # F9 (live-feedback 2026-07-09): "Show model thinking" toggle.
        # When checked, the next user-message carries show_thinking=True and the
        # dock renders the model's reasoning-channel tokens as a collapsible grey
        # block above each answer. Default ON.
        thinking_row = QHBoxLayout()
        self.thinking_checkbox = QCheckBox("Show model thinking")
        self.thinking_checkbox.setChecked(self.settings.show_thinking)
        self.thinking_checkbox.toggled.connect(self._on_thinking_toggled)
        thinking_row.addWidget(self.thinking_checkbox)
        outer.addLayout(thinking_row)
        self.aoi_status = QLabel(aoi.aoi_status_text(None, False))
        self.aoi_status.setStyleSheet(_STATUS_LINE_STYLE)
        outer.addWidget(self.aoi_status)
        self._refresh_aoi_status()

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

    # -- AOI ------------------------------------------------------------------ #

    def _rect_to_bbox4326(
        self, extent, authid: str
    ) -> Optional[Tuple[float, float, float, float]]:
        """A QgsRectangle in ``authid`` -> EPSG:4326 bbox tuple, or None.

        Pure math (``aoi.extent_to_bbox4326``) covers EPSG:4326/3857; any
        other CRS falls back to QGIS's own transform. Never raises -- an
        unresolvable CRS yields None (the status line says so honestly).
        """
        bbox = aoi.extent_to_bbox4326(
            extent.xMinimum(), extent.yMinimum(),
            extent.xMaximum(), extent.yMaximum(), authid,
        )
        if bbox is not None:
            return bbox
        # Arbitrary CRS: use QGIS's transform machinery.
        try:
            from qgis.core import (
                QgsCoordinateReferenceSystem,
                QgsCoordinateTransform,
                QgsProject,
            )

            transform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem(authid),
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance(),
            )
            rect = transform.transformBoundingBox(extent)
            return aoi.extent_to_bbox4326(
                rect.xMinimum(), rect.yMinimum(),
                rect.xMaximum(), rect.yMaximum(), "EPSG:4326",
            )
        except Exception:  # noqa: BLE001 -- honest None, noted in status
            return None

    def _canvas_bbox4326(self) -> Optional[Tuple[float, float, float, float]]:
        """Current canvas extent as an EPSG:4326 bbox tuple, or None."""
        try:
            canvas = self.iface.mapCanvas()
            extent = canvas.extent()
            authid = canvas.mapSettings().destinationCrs().authid()
        except Exception:  # noqa: BLE001 -- no canvas (headless), no AOI
            return None
        return self._rect_to_bbox4326(extent, authid)

    def _selection_bbox4326(self) -> Optional[Tuple[float, float, float, float]]:
        """The active layer's SELECTION bbox as EPSG:4326, or None.

        Milestone 3 item 4 (v1): the bbox OF the selection, not the exact
        ring -- the agent's structured AOI carriers (``args.bbox`` on
        case-create + the in-text line) are 4-number boxes. None when there
        is no active vector layer, no selected features, or a degenerate
        rect (a single point selection has no area -- honest None).
        """
        try:
            layer = self.iface.activeLayer()
            if layer is None or not hasattr(layer, "selectedFeatureCount"):
                return None
            if layer.selectedFeatureCount() == 0:
                return None
            rect = layer.boundingBoxOfSelected()
            if rect is None or rect.isEmpty():
                return None
            authid = layer.crs().authid()
        except Exception:  # noqa: BLE001 -- no selection resolvable, no AOI
            return None
        return self._rect_to_bbox4326(rect, authid)

    def _aoi_for_send(
        self,
    ) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[str]]:
        """The ``(bbox, source)`` to attach right now, or ``(None, None)``
        (off / unresolved / too big). Also refreshes the status line so the
        user sees WHY. Selection (when its toggle is on and features are
        selected) overrides the canvas extent -- see ``aoi.choose_aoi``."""
        canvas_enabled = self.aoi_checkbox.isChecked()
        selection_enabled = self.selection_checkbox.isChecked()
        selection_bbox = self._selection_bbox4326() if selection_enabled else None
        canvas_bbox = self._canvas_bbox4326() if canvas_enabled else None
        bbox, source = aoi.choose_aoi(selection_bbox, canvas_bbox, selection_enabled)
        enabled = canvas_enabled or selection_enabled
        self.aoi_status.setText(
            aoi.aoi_status_text(bbox, enabled, source=source or "canvas")
        )
        if bbox is not None and aoi.bbox_within_guard(bbox):
            return bbox, source
        return None, None

    def _refresh_aoi_status(self) -> None:
        self._aoi_for_send()

    def _on_aoi_toggled(self, checked: bool) -> None:
        self.settings.canvas_aoi = checked
        self._refresh_aoi_status()

    def _on_selection_aoi_toggled(self, checked: bool) -> None:
        self.settings.selection_aoi = checked
        self._refresh_aoi_status()

    def _on_thinking_toggled(self, checked: bool) -> None:
        """F9 (live-feedback 2026-07-09): persist the show_thinking preference."""
        self.settings.show_thinking = checked

    # -- connection ----------------------------------------------------------- #

    def _wire_bridge(self) -> None:
        self.bridge.connected.connect(self._on_connected)
        self.bridge.case_ready.connect(self._on_case_ready)
        # ``agent_event`` (never ``event`` -- that name shadows the C++
        # virtual QObject.event() and qFatals QGIS; see ws_bridge).
        self.bridge.agent_event.connect(self._on_event)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.closed.connect(self._on_closed)
        self.bridge.reconnecting.connect(self._on_reconnecting)
        self.bridge.resumed.connect(self._on_resumed)
        self.bridge.auth_expired.connect(self._on_auth_expired)

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
        self._session_case_title = title
        anon = self.settings.anonymous_user_id or None
        # #170 AOI-first: create the case WITH the canvas/selection extent so
        # the very first turn's _turn_case_bbox anchors on it (guard applies).
        case_bbox, _source = self._aoi_for_send()
        self.bridge.start(
            url,
            token=self.settings.effective_token(),
            anonymous_user_id=anon if self.settings.mode == MODE_LOCAL else None,
            case_title=title,
            case_bbox=list(case_bbox) if case_bbox else None,
        )

    def disconnect_agent(self) -> None:
        self.bridge.stop()
        self._connected = False
        self._case_id = None
        self._set_case_label("")
        self._set_dot("disconnected")
        self.status_label.setText("Not connected")
        self.connect_btn.setText("Connect")

    def _set_case_label(self, title: str) -> None:
        self._case_title = title
        self.case_label.setText(f"Case: {title}" if title else "")
        self.case_label.setVisible(bool(title))

    def _toggle_connection(self) -> None:
        if self.bridge.running:
            self.disconnect_agent()
        else:
            self.connect_agent()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()

    def _open_cases(self) -> None:
        dlg = CasesDialog(self, self._cases)
        self._cases_dialog = dlg
        try:
            dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        finally:
            self._cases_dialog = None

    # -- case switching (milestone 3) ------------------------------------------ #

    def select_case(self, case_id: str, title: str) -> None:
        """Switch the chat session to an existing case (case-command select).

        The server replies with a full ``case-open`` rehydration; the dock
        rebinds on that event (authoritative title + replayed layers), so
        this only stamps optimistically and sends.
        """
        if not self.bridge.running:
            self.status_label.setText("Not connected -- press Connect first")
            return
        self._case_id = case_id
        self._note(f"Switching to case '{title}' ...")
        self.bridge.select_case(case_id)

    def refresh_cases(self) -> str:
        """Debounced case-list refresh (one session-resume round trip -- see
        ``trid3nt_client.request_case_list_refresh`` for the tradeoff).
        Returns a status line for the Cases dialog."""
        if not self._refresh_debounce.allow():
            return "Refresh debounced -- try again in a moment"
        if self.bridge.refresh_case_list():
            return "Refreshing case list ..."
        return "Not connected -- the list refreshes on the next connect"

    # -- open case in QGIS ------------------------------------------------------ #

    def open_case_in_qgis(self, case_id: str, label: str) -> None:
        """Export ``case_id`` via /api/export-qgis and add the produced layers
        to the CURRENT project (see ``case_export`` for why we add layers
        instead of opening the .qgz).

        Local mode talks to the local agent's HTTP listener and reads the
        artifact paths directly. Remote mode (milestone 3) POSTs the same
        route on the HTTP base derived from the remote WS URL, then downloads
        the .gpkg/.qgz through GET /api/export-qgis/file into a temp dir.
        """
        if self.settings.mode == MODE_LOCAL:
            base_url = self.settings.export_api
            remote = False
            self._note(f"Exporting case '{label}' via the local agent ...")
        else:
            base_url = case_export.ws_url_to_http_base(self.settings.remote_url)
            remote = True
            self._note(
                f"Exporting case '{label}' on the remote agent "
                f"({base_url}) -- artifacts download to a local temp dir ..."
            )
        task = _ExportTask(base_url, case_id, self, remote=remote)
        task.finished.connect(self._on_export_finished)
        task.errored.connect(self._on_export_errored)
        self._export_tasks.append(task)
        task.start()

    def _on_export_finished(self, case_id: str, result: dict) -> None:
        plan = case_export.plan_export_layers(result)
        notes = self.materializer.materialize_export(plan, group_label=case_id[:8])
        for note in notes:
            self._note(note)
        if plan.qgz_path:
            self._note(f"Styled QGIS project also written: {plan.qgz_path}")
        self._scroll_to_bottom()

    def _on_export_errored(self, case_id: str, message: str) -> None:
        self._note(f"Case export failed: {message}", error=True)

    def _note(self, text: str, error: bool = False) -> None:
        self._ensure_pending().add_note(text, error=error)
        self._scroll_to_bottom()

    # -- bridge slots (UI thread) ---------------------------------------------- #

    def _on_connected(self, user_id: str, is_anonymous: bool) -> None:
        self._connected = True
        if is_anonymous and self.settings.mode == MODE_LOCAL:
            self.settings.anonymous_user_id = user_id
        self.status_label.setText(f"Connected ({'anonymous' if is_anonymous else user_id})")

    def _on_case_ready(self, case_id: str) -> None:
        self._case_id = case_id
        self.materializer.set_case(case_id, self._session_case_title or None)
        self._set_case_label(self._session_case_title or case_id[:8])
        self._set_dot("connected")
        self.status_label.setText(f"Connected -- case {case_id[:8]}")

    def _on_failed(self, message: str) -> None:
        self._connected = False
        self._set_dot("error")
        self.status_label.setText(f"Connection failed: {message}")
        self.connect_btn.setText("Connect")

    def _on_auth_expired(self, message: str) -> None:
        """The token was rejected (broker 401/403 or in-band AUTH_REQUIRED):
        the worker has STOPPED -- no silent reconnect loop. Say exactly what
        to do next."""
        self._connected = False
        self._set_dot("error")
        self.status_label.setText(
            "Token expired or rejected -- paste a fresh one in Settings"
        )
        self.connect_btn.setText("Connect")
        self._note(f"Authentication failed: {message}", error=True)

    def _on_closed(self, reason: str) -> None:
        self._connected = False
        self._case_id = None
        if reason == "auth-expired":
            pass  # _on_auth_expired already painted the honest status
        elif reason != "stopped":
            self._set_dot("error")
            self.status_label.setText(f"Disconnected: {reason}")
        self.connect_btn.setText("Connect")

    def _on_reconnecting(self, reason: str) -> None:
        # Transport lost; the worker's capped-jitter ladder is running.
        self._connected = False
        self._set_dot("connecting")
        self.status_label.setText("Connection lost -- reconnecting ...")

    def _on_resumed(self) -> None:
        self._connected = True
        self._set_dot("connected")
        suffix = f" -- case {self._case_id[:8]}" if self._case_id else ""
        self.status_label.setText(f"Reconnected{suffix}")

    def _on_event(self, kind: str, data: object) -> None:
        if not isinstance(data, dict):
            return
        if kind == "thinking-chunk":
            # F9 (live-feedback 2026-07-09): local model reasoning-channel token.
            # Accumulate into the pending entry's thinking block; the block
            # collapses automatically when the first answer delta arrives.
            entry = self._ensure_pending()
            entry.append_thinking_delta(str(data.get("delta") or ""))
            self._scroll_to_bottom()
        elif kind == "chunk":
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
            self._show_gate_card(data)
        elif kind == "case-open":
            self._on_case_open_event(data)
        elif kind == "case-list":
            cases = data.get("cases")
            if isinstance(cases, list):
                self._cases = [c for c in cases if isinstance(c, CaseInfo)]
                if self._cases_dialog is not None:
                    self._cases_dialog.set_cases(self._cases)
        elif kind == "turn-complete":
            if self._pending is not None:
                self._pending = None

    def _on_case_open_event(self, payload: dict) -> None:
        """A ``case-open`` rehydration arrived (the select response --
        create's case-open is consumed inside the worker handshake).

        Rebinds the dock: authoritative title in the header, a FRESH layer
        group named for the case (dedup reset), and the persisted
        loaded_layers replayed into it.
        """
        info = parse_case_open(payload)
        if info is None:
            self._note(
                "Case switch failed: the server could not rehydrate the case "
                "(archived/deleted between list and select?)",
                error=True,
            )
            return
        self._case_id = info.case_id
        self._pending = None
        self.materializer.set_case(info.case_id, info.title)
        self._set_case_label(info.title)
        self._set_dot("connected")
        self.status_label.setText(f"Connected -- case {info.case_id[:8]}")
        self._note(f"Case '{info.title}' active")
        if info.layers:
            notes = self.materializer.materialize(info.layers)
            for note in notes:
                self._note(note)
        self._scroll_to_bottom()

    # -- gate card -------------------------------------------------------------- #

    def _show_gate_card(self, payload: dict) -> None:
        warning = gate.parse_payload_warning(payload)
        if warning is None:
            self._note(
                "Received a malformed tool-payload-warning (no warning_id) -- "
                "cannot confirm it; the run will time out server-side.",
                error=True,
            )
            return
        card = GateCard(warning, self._on_gate_decision)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._scroll_to_bottom()

    def _on_gate_decision(
        self, warning_id: str, decision: str, revised_args: Optional[dict]
    ) -> None:
        try:
            self.bridge.confirm_payload(warning_id, decision, revised_args)
        except Exception as exc:  # noqa: BLE001
            self._note(f"confirmation send failed: {exc}", error=True)

    # -- sending ------------------------------------------------------------- #

    def _send(self) -> None:
        text = self.input_edit.text().strip()
        if not text:
            return
        if not (self._case_id and self.bridge.running):
            self.status_label.setText("Not connected -- press Connect first")
            return
        self.input_edit.clear()
        self._add_user_bubble(text)
        self._pending = _AssistantEntry(self.messages_layout)
        self._scroll_to_bottom()
        bbox, source = self._aoi_for_send()
        wire_text = (
            aoi.attach_aoi_to_text(text, bbox, source=source or "canvas")
            if bbox
            else text
        )
        try:
            # F9: pass show_thinking so the server enables reasoning-channel
            # forwarding for this turn (local mode only; remote ignores the field).
            self.bridge.send_chat(wire_text, show_thinking=self.settings.show_thinking)
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"send failed: {exc}", error=True)

    # -- teardown ------------------------------------------------------------- #

    def shutdown(self) -> None:
        self.bridge.stop()
