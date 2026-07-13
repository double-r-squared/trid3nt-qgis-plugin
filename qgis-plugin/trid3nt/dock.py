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
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import aoi, case_export, gate, probe, push_layer
from .layers import LayerMaterializer, ensure_basemap, zoom_to_bbox4326, zoom_to_extent
from .plugin_settings import MODE_LOCAL, MODE_REMOTE, PluginSettings
from .trid3nt_client import (
    CaseInfo,
    CaseListRequestError,
    Debouncer,
    PipelineStep,
    fetch_case_list,
    find_fallback_bbox,
    parse_case_open,
)
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
    """Mode local/remote, URLs, pasted token, MinIO + export API endpoints,
    AOI toggles, and the auto-basemap toggle.

    Item 4 (live-feedback 2026-07-09): the AOI checkboxes (canvas / selected
    polygon) used to live-apply straight from the dock; they now live here
    ONLY, and nothing applies until Save -- every field, line edits and
    checkboxes alike, copies into ``settings`` in the ``accept()`` branch.
    """

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

        # Item 4: AOI toggles + auto-basemap moved here from the dock (they
        # used to live-apply; now they apply only on Save, like every other
        # field in this dialog).
        self.canvas_aoi_checkbox = QCheckBox("Use map canvas as area of interest")
        self.canvas_aoi_checkbox.setChecked(settings.canvas_aoi)
        form.addRow("AOI", self.canvas_aoi_checkbox)

        self.selection_aoi_checkbox = QCheckBox(
            "Use selected polygon as AOI (overrides canvas)"
        )
        self.selection_aoi_checkbox.setChecked(settings.selection_aoi)
        form.addRow("", self.selection_aoi_checkbox)

        self.auto_basemap_checkbox = QCheckBox(
            "Add OpenStreetMap basemap automatically"
        )
        self.auto_basemap_checkbox.setChecked(settings.auto_basemap)
        form.addRow("Basemap", self.auto_basemap_checkbox)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
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
        self._settings.canvas_aoi = self.canvas_aoi_checkbox.isChecked()
        self._settings.selection_aoi = self.selection_aoi_checkbox.isChecked()
        self._settings.auto_basemap = self.auto_basemap_checkbox.isChecked()
        super().accept()


# Style constants for the thinking block (F9, live-feedback 2026-07-09).
_THINKING_TOGGLE_STYLE = "color: palette(mid); font-size: 8pt; border: none; text-align: left;"
_THINKING_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid palette(mid); "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: palette(mid);"
)
# Probe-panel error variant (BUG 3b, live-feedback 2026-07-12): the same
# block chrome as the thinking body but in the error red, so a failed probe
# is unmistakable without landing in chat.
_PROBE_ERROR_BLOCK_STYLE = (
    "background-color: palette(window); border-left: 2px solid #f85149; "
    "border-radius: 2px; padding: 4px 6px; font-size: 8pt; color: #f85149;"
)


class _WrapLabel(QLabel):
    """Word-wrapping QLabel whose WRAPPED height the layouts actually honor.

    BUG 1 (live-feedback 2026-07-12): "show me the landcover over washington
    state" painted as ONE clipped visual line in the user bubble. Classic Qt
    wrapped-label clip: QBoxLayout gives an alignment-constrained item its
    sizeHint height -- computed at the UNWRAPPED width -- so the label's real
    wrapped height (heightForWidth of the width it actually got) is never
    honored (measured pre-fix: 73px painted vs 133px needed at a 320px-wide
    dock; the assistant bubble clipped the same way, 153px vs 193px). Fix:
    re-assert minimumHeight from heightForWidth(actual width) on every
    resize/setText -- minimum sizes propagate through every layout +
    scroll-area combination even where height-for-width does not.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def _sync_min_height(self) -> None:
        width = self.width()
        if width <= 0:
            return
        wrapped = self.heightForWidth(width)
        if wrapped > 0 and wrapped != self.minimumHeight():
            self.setMinimumHeight(wrapped)

    def setText(self, text: str) -> None:  # noqa: N802 -- Qt-mandated name
        super().setText(text)
        self._sync_min_height()

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        super().resizeEvent(event)
        self._sync_min_height()


def _is_error_note(note: str) -> bool:
    """BUG 3a (live-feedback 2026-07-12): materializer notes are plain
    strings, so classify by the honest failure vocabulary layers.py uses --
    error-ish notes must stay VISIBLE outside the collapsed Layers toggle."""
    lowered = note.lower()
    return any(
        token in lowered
        for token in ("fail", "error", "skipp", "reject", "unknown")
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

        self._thinking_label = _WrapLabel("")
        self._thinking_label.setTextFormat(Qt.PlainText)
        self._thinking_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._thinking_label.setStyleSheet(_THINKING_BLOCK_STYLE)
        thinking_lay.addWidget(self._thinking_label)

        self._thinking_container.setVisible(False)
        lay.addWidget(self._thinking_container)
        self._thinking_text = ""

        self.label = _WrapLabel("")
        self.label.setTextFormat(Qt.PlainText)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setStyleSheet(_ASSISTANT_BUBBLE_STYLE)
        self.label.setVisible(False)  # only once NON-whitespace text arrives
        lay.addWidget(self.label, 0, Qt.AlignLeft)

        # Transient pipeline lines (replaced on every pipeline-state frame).
        self.pipeline_area = QVBoxLayout()
        self.pipeline_area.setSpacing(0)
        lay.addLayout(self.pipeline_area)

        # BUG 3a (live-feedback 2026-07-12, NATE: layer notes "should show
        # up somewhere else or collapse because they are in the way and push
        # the last prompt or response way up in the chat"): per-layer
        # materialization notes fold into ONE "Layers (N)" toggle styled
        # like the thinking toggle, DEFAULT COLLAPSED. Error notes never
        # land here -- ``add_layer_notes`` routes them to ``add_note`` so
        # failures stay visible.
        self._layers_toggle = QPushButton("Layers (0)")
        self._layers_toggle.setFlat(True)
        self._layers_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self._layers_toggle.setCheckable(True)
        self._layers_toggle.setChecked(False)  # default collapsed
        self._layers_toggle.clicked.connect(self._toggle_layer_notes)
        self._layers_toggle.setVisible(False)
        lay.addWidget(self._layers_toggle)
        self._layers_body = QWidget()
        self._layers_body_lay = QVBoxLayout(self._layers_body)
        self._layers_body_lay.setContentsMargins(8, 0, 0, 0)
        self._layers_body_lay.setSpacing(0)
        self._layers_body.setVisible(False)
        lay.addWidget(self._layers_body)
        self._layers_count = 0

        # Persistent notes (layer adds, errors) -- append-only.
        self.notes_area = QVBoxLayout()
        self.notes_area.setSpacing(0)
        lay.addLayout(self.notes_area)

        # Insert above the terminal stretch.
        parent_layout.insertWidget(parent_layout.count() - 1, self.container)
        self.text = ""
        # BUG 2 (live-feedback 2026-07-12): True once the FIRST non-whitespace
        # answer token arrived (reveals the bubble + collapses thinking).
        self._answer_started = False

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
        # BUG 2 (live-feedback 2026-07-12): qwen3 emits whitespace-only text
        # deltas after </think>, and revealing the bubble on ANY delta
        # painted an empty grey box on thinking+tool-only turns. Reveal the
        # label (and collapse the thinking block) only on the first
        # NON-whitespace content; a turn whose text is all whitespace keeps
        # the bubble hidden and the thinking block expanded.
        self.text += delta
        if not self.text.strip():
            return
        if not self._answer_started:
            self._answer_started = True
            if self._thinking_text:
                # First NON-whitespace answer token: collapse the thinking
                # block (was: first token of any kind).
                self.collapse_thinking()
        # Display-side lstrip only: the whitespace-only prefix qwen3 emits
        # would otherwise pad the top of the bubble with blank lines.
        self.label.setText(self.text.lstrip())
        self.label.setVisible(True)

    def set_pipeline_lines(self, lines: List[str]) -> None:
        while self.pipeline_area.count():
            item = self.pipeline_area.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for line in lines:
            lbl = _WrapLabel(line)
            lbl.setTextFormat(Qt.PlainText)
            lbl.setStyleSheet(_STATUS_LINE_STYLE)
            self.pipeline_area.addWidget(lbl)

    def add_note(self, text: str, error: bool = False) -> None:
        lbl = _WrapLabel(text)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setStyleSheet(_ERROR_LINE_STYLE if error else _STATUS_LINE_STYLE)
        self.notes_area.addWidget(lbl)

    # -- collapsed layer-note batch (BUG 3a, live-feedback 2026-07-12) ------ #

    def _toggle_layer_notes(self) -> None:
        self._layers_body.setVisible(self._layers_toggle.isChecked())

    def add_layer_notes(self, notes: List[str]) -> None:
        """Fold a batch of layer-materialization notes into the collapsed
        "Layers (N)" toggle. Error-ish notes (``_is_error_note``) go through
        ``add_note(error=True)`` instead -- never swallowed by the collapse.
        Repeated batches on the same entry extend the count in place."""
        for note in notes:
            if _is_error_note(note):
                self.add_note(note, error=True)
                continue
            lbl = _WrapLabel(note)
            lbl.setTextFormat(Qt.PlainText)
            lbl.setStyleSheet(_STATUS_LINE_STYLE)
            self._layers_body_lay.addWidget(lbl)
            self._layers_count += 1
        if self._layers_count:
            self._layers_toggle.setText(f"Layers ({self._layers_count})")
            self._layers_toggle.setVisible(True)
            self._layers_body.setVisible(self._layers_toggle.isChecked())


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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Item 5 (live-feedback 2026-07-09): the collapsed one-line summary,
        # shown only once the card is answered (``_collapse``). "show
        # details" re-expands ``self._body`` read-only (its buttons stay
        # disabled -- see ``_commit``).
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.PlainText)
        self.summary_lbl.setStyleSheet(_GATE_TITLE_STYLE)
        summary_row.addWidget(self.summary_lbl, 1)
        self.details_toggle = QPushButton("show details")
        self.details_toggle.setFlat(True)
        self.details_toggle.setCheckable(True)
        self.details_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self.details_toggle.clicked.connect(self._toggle_details)
        summary_row.addWidget(self.details_toggle)
        self._summary_container = QWidget()
        self._summary_container.setLayout(summary_row)
        self._summary_container.setVisible(False)
        outer.addWidget(self._summary_container)

        # The full card content -- visible until answered, then hidden
        # behind the summary line (re-expandable via "show details").
        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

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
        self._collapse()

    # -- collapse (item 5, live-feedback 2026-07-09) ---------------------------- #

    def _collapse(self) -> None:
        """Fold to a single amber summary line once answered. The body stays
        intact underneath (its buttons already disabled by ``_commit``) so
        "show details" can re-expand a read-only view."""
        chosen = self._chosen_resolution()
        if self._decided == "proceed" and chosen is not None:
            line = f"Resolution gate: proceeded at {chosen:g} m"
        elif self._decided == "proceed":
            line = "Resolution gate: proceeded"
        elif self._decided == "cancel":
            line = "Resolution gate: cancelled"
        elif self._decided == "narrow_scope":
            line = "Resolution gate: proceeded with overrides"
        else:
            line = f"Resolution gate: {self._decided or 'answered'}"
        self.summary_lbl.setText(line)
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")


class _ExportTask(QObject):
    """POST /api/export-qgis off the UI thread (cross-thread signal emit).

    ``remote=True`` (milestone 3 item 1) additionally downloads the produced
    .gpkg/.qgz through GET /api/export-qgis/file into a fresh local temp dir
    and rewrites the result's paths to the local copies, so the finished
    slot can plan layers exactly like local mode.

    Mesh artifacts (MDAL phase 1) never travel through the ``output_dir``
    copy the .gpkg/.tif entries get -- the result's ``mesh`` list only
    carries an ``s3_uri`` (see ``case_export`` module docstring), so BOTH
    modes need their own fetch. Local mode reads MinIO directly
    (``minio_endpoint``, network-reachable); remote mode has no
    presigned-fetch path yet, so its mesh entries are left un-downloaded and
    ``plan_export_layers`` turns that into an honest skip note.
    """

    finished = pyqtSignal(str, dict)  # case_id, result (localized if remote)
    errored = pyqtSignal(str, str)    # case_id, message

    def __init__(
        self,
        base_url: str,
        case_id: str,
        parent: Optional[QObject] = None,
        remote: bool = False,
        minio_endpoint: str = "",
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._case_id = case_id
        self._remote = remote
        self._minio_endpoint = minio_endpoint

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
            elif result.get("mesh"):
                mesh_dir = tempfile.mkdtemp(prefix="trid3nt_mesh_export_")
                result = case_export.localize_mesh_entries(
                    result, self._minio_endpoint, mesh_dir
                )
        except case_export.ExportRequestError as exc:
            self.errored.emit(self._case_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(self._case_id, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(self._case_id, result)


class _CaseListTask(QObject):
    """GET /api/case-list off the UI thread (items b/c, live-feedback
    2026-07-09) -- follows the ``_ExportTask`` pattern (cross-thread signal
    emit) so a slow/dead agent HTTP listener never freezes the Cases dialog.
    """

    finished = pyqtSignal(list)  # list[CaseInfo]
    errored = pyqtSignal(str)    # honest message

    def __init__(self, base_url: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._base_url = base_url

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            cases = fetch_case_list(self._base_url)
        except CaseListRequestError as exc:
            self.errored.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(cases)


class _PushLayerTask(QObject):
    """Push the active QGIS layer into a case via ``push_layer.py``, off the
    UI thread (cross-thread signal emit) -- follows the ``_ExportTask``
    pattern. One task = one export-to-tempfile + upload + register round
    trip (``push_layer.push_active_layer``); the temp file is deleted by
    ``push_exported_file`` whether the ingest POST succeeds or fails.
    """

    finished = pyqtSignal(str, dict)  # layer_name, result
    errored = pyqtSignal(str, str)    # layer_name, message

    def __init__(
        self,
        base_url: str,
        case_id: str,
        layer,
        make_aoi: bool = False,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._case_id = case_id
        self._layer = layer
        self._make_aoi = make_aoi
        try:
            self._layer_name = layer.name() or ""
        except Exception:  # noqa: BLE001 -- best-effort label only
            self._layer_name = ""

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = push_layer.push_active_layer(
                self._base_url, self._case_id, self._layer, make_aoi=self._make_aoi
            )
        except push_layer.PushLayerRequestError as exc:
            self.errored.emit(self._layer_name, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(self._layer_name, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(self._layer_name, result)


class _ProbePointTask(QObject):
    """POST /api/probe-point off the UI thread, for one map click -- follows
    the ``_ExportTask`` / ``_PushLayerTask`` pattern (cross-thread signal
    emit). One task = one round trip (``probe.post_probe_point``); the
    result formatting (``probe.format_probe_result``) runs back on the UI
    thread in the ``finished`` slot, matching every other worker task here.
    """

    finished = pyqtSignal(float, float, dict)  # lon, lat, result
    errored = pyqtSignal(float, float, str)    # lon, lat, message

    def __init__(
        self,
        base_url: str,
        case_id: str,
        lon: float,
        lat: float,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._case_id = case_id
        self._lon = lon
        self._lat = lat

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = probe.post_probe_point(
                self._base_url, self._case_id, self._lon, self._lat
            )
        except probe.ProbePointRequestError as exc:
            self.errored.emit(self._lon, self._lat, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
            self.errored.emit(self._lon, self._lat, f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(self._lon, self._lat, result)


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
        self.listw.itemClicked.connect(self._open_item)
        self.listw.itemDoubleClicked.connect(self._open_item)
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
        export_action = menu.addAction("Export GeoTIFFs")
        delete_action = menu.addAction("Delete")
        global_pos = self.listw.viewport().mapToGlobal(pos)
        chosen = menu.exec_(global_pos) if hasattr(menu, "exec_") else menu.exec(global_pos)
        if chosen is export_action:
            self._dock.open_case_in_qgis(case_id, str(title))
            self.accept()
        elif chosen is delete_action:
            self._delete_case(case_id, str(title))

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
        self._case_list_tasks: List[_CaseListTask] = []  # keep-alive refs
        self._push_tasks: List[_PushLayerTask] = []  # keep-alive refs
        self._probe_tasks: List[_ProbePointTask] = []  # keep-alive refs
        # The Probe map tool (design point 2): built lazily on first toggle-on
        # (QgsMapToolEmitPoint needs a live canvas). ``_prev_map_tool`` is the
        # canvas' tool saved right before the Probe tool is installed, so
        # toggling off restores it (never steals the tool permanently).
        self._probe_map_tool = None
        self._prev_map_tool = None
        self._refresh_debounce = Debouncer()
        # Item d (live-feedback 2026-07-09): a case picked from the Cases
        # dialog before/while connecting -- opened via ``_on_case_ready``
        # once the (auto-)connect actually completes, so a cold-list click
        # is never silently dropped.
        self._pending_open_case: Optional[Tuple[str, str]] = None
        # AUTO-CONNECT (live-feedback 2026-07-09): fires once per dock SHOW,
        # reset on hide -- see ``showEvent``/``hideEvent``/``_auto_connect_local_once``.
        self._auto_connect_done_this_show = False

        self._build_ui()
        self._wire_bridge()

    # -- Qt lifecycle -------------------------------------------------------- #

    def showEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        super().showEvent(event)
        self._auto_connect_local_once()

    def hideEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        super().hideEvent(event)
        self._auto_connect_done_this_show = False

    def _auto_connect_local_once(self) -> None:
        """AUTO-CONNECT (live-feedback 2026-07-09): cases must be visible
        WITHOUT the user pressing Connect, and the dock should not require a
        manual connect at all in local mode. Fires once per dock show (reset
        on hide, so re-opening the dock tries again); never retries on
        failure within one show -- a failed attempt just paints the existing
        honest status line via ``connect_agent``'s own failure path, exactly
        like a manual click would. Remote mode is unaffected (manual connect
        only, a pasted token is required)."""
        if self._auto_connect_done_this_show:
            return
        self._auto_connect_done_this_show = True
        if self.settings.mode != MODE_LOCAL:
            return
        if self.bridge.running:
            return
        self.connect_agent()

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

        # Item 3 (live-feedback 2026-07-09): header shortcut for a fresh
        # case, next to Cases -- the case-open reply rebinds the dock
        # (fresh layer group, header title) via the existing handler.
        self.new_case_btn = QToolButton()
        self.new_case_btn.setText("New")
        self.new_case_btn.setToolTip("Start a fresh case")
        self.new_case_btn.clicked.connect(self.new_case)
        header.addWidget(self.new_case_btn)

        # Bidirectional layer push: send iface.activeLayer() into the
        # current case as a first-class input layer (the reverse seam of
        # "Open in QGIS").
        self.push_layer_btn = QToolButton()
        self.push_layer_btn.setText("Push layer")
        self.push_layer_btn.setToolTip(
            "Send the active QGIS layer to the current case"
        )
        self.push_layer_btn.clicked.connect(self._push_active_layer)
        header.addWidget(self.push_layer_btn)

        # Map-click point probe: click the canvas to sample every raster
        # layer (and detected animation-frame sequence) on the current case
        # at that point. Checkable -- ON installs a QgsMapToolEmitPoint on
        # the canvas (saving whatever tool was active so toggling off
        # restores it); OFF restores the saved tool.
        self.probe_btn = QToolButton()
        self.probe_btn.setText("Probe")
        self.probe_btn.setCheckable(True)
        self.probe_btn.setToolTip(
            "Click the map to sample the case's layers at a point"
        )
        self.probe_btn.toggled.connect(self._toggle_probe_tool)
        header.addWidget(self.probe_btn)

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

        # BUG 3b (live-feedback 2026-07-12, NATE verbatim: "the probe should
        # show the data somewhere else this should not show up in chat
        # period"): probe output renders HERE -- a collapsible panel pinned
        # under the message list, near the Probe toggle's effect -- and is
        # REPLACED in place on each map click. Nothing probe-related is
        # added to the chat message list anymore (results, in-flight status,
        # and errors alike). Hidden until the first probe interaction.
        self._probe_panel = QWidget()
        probe_lay = QVBoxLayout(self._probe_panel)
        probe_lay.setContentsMargins(0, 0, 0, 0)
        probe_lay.setSpacing(0)
        self.probe_results_toggle = QPushButton("Probe results")
        self.probe_results_toggle.setFlat(True)
        self.probe_results_toggle.setCheckable(True)
        self.probe_results_toggle.setChecked(True)  # expanded while probing
        self.probe_results_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self.probe_results_toggle.clicked.connect(self._toggle_probe_results)
        probe_lay.addWidget(self.probe_results_toggle)
        self.probe_result_label = _WrapLabel("")
        self.probe_result_label.setTextFormat(Qt.PlainText)
        self.probe_result_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.probe_result_label.setStyleSheet(_THINKING_BLOCK_STYLE)
        probe_lay.addWidget(self.probe_result_label)
        self._probe_panel.setVisible(False)
        outer.addWidget(self._probe_panel)

        # Item 4 (live-feedback 2026-07-09): the AOI toggles (canvas /
        # selected polygon) moved into Settings -- apply-on-Save there now,
        # instead of live-applying from checkboxes here. ``self.aoi_status``
        # below stays as the compact read-only status line, refreshed from
        # settings at build time, on every send, and after Settings closes.
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
        lbl = _WrapLabel(text)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet(_USER_BUBBLE_STYLE)
        # BUG 1 (live-feedback 2026-07-12): the bare QSizePolicy(h, v) ctor
        # DROPS the height-for-width flag QLabel.setWordWrap had set, which
        # (with the AlignRight cell) clipped long messages to one visual
        # line. Restore the flag so the layout asks the label how tall its
        # wrapped text is; _WrapLabel's min-height re-assert covers the
        # layout paths that still ignore height-for-width. Horizontal
        # Maximum + AlignRight keep the hug-the-text right-aligned look.
        policy = QSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        lbl.setSizePolicy(policy)
        lay.addWidget(lbl, 0, Qt.AlignRight)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, container)
        self._scroll_to_bottom()

    def _ensure_pending(self) -> _AssistantEntry:
        if self._pending is None:
            self._pending = _AssistantEntry(self.messages_layout)
        return self._pending

    def _clear_messages(self) -> None:
        """ITEM B (case switch must clear chat): remove every message-list
        child widget (user bubbles, assistant entries, gate cards, notes)
        while keeping the terminal stretch item ``_build_ui`` adds last --
        every insertion elsewhere in the dock inserts BEFORE it, so it must
        stay. Also drops any pending streaming assistant entry; a stale
        target from the previous case must never receive the new case's
        deltas."""
        self._pending = None
        # BUG 3b (live-feedback 2026-07-12): the probe panel shows CASE
        # data -- a table from the previous case must not linger across a
        # switch. Hide it (its next click repopulates it).
        self._probe_panel.setVisible(False)
        self.probe_result_label.setText("")
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _replay_chat_history(self, messages: List[dict]) -> None:
        """ITEM B: repaint a just-opened case's persisted conversation
        (plain user/assistant bubbles, no thinking/pipeline chrome -- this
        is a read-only replay, not a live stream) before the "Case '<title>'
        active" note. ``messages`` is already role/content-filtered and
        capped by ``trid3nt_client.parse_chat_history``; an empty list (a
        brand-new case) is a no-op -- clean slate, just the active note."""
        for row in messages:
            role = row.get("role")
            content = row.get("content")
            if not isinstance(content, str) or not content:
                continue
            if role == "user":
                self._add_user_bubble(content)
            elif role == "agent":
                entry = _AssistantEntry(self.messages_layout)
                entry.append_delta(content)

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
        selected) overrides the canvas extent -- see ``aoi.choose_aoi``.

        Item 4 (live-feedback 2026-07-09): the two toggles now live ONLY in
        Settings (apply-on-Save), so this reads them straight off
        ``self.settings`` instead of dock checkboxes.
        """
        canvas_enabled = self.settings.canvas_aoi
        selection_enabled = self.settings.selection_aoi
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

    def _on_thinking_toggled(self, checked: bool) -> None:
        """F9 (live-feedback 2026-07-09): persist the show_thinking preference."""
        self.settings.show_thinking = checked

    # -- probe (map-click point sample) --------------------------------------- #

    def _point_to_lonlat4326(
        self, point, authid: str
    ) -> Optional[Tuple[float, float]]:
        """A clicked ``QgsPointXY`` in ``authid`` -> EPSG:4326 ``(lon, lat)``,
        or None.

        Pure math (``aoi.merc_to_lonlat``) covers EPSG:4326 passthrough +
        EPSG:3857; any other CRS falls back to QGIS's own transform --
        mirrors ``_rect_to_bbox4326``. Never raises -- an unresolvable CRS
        or an out-of-range result yields None (the click note says so
        honestly)."""
        import math

        authid_norm = (authid or "").strip().upper()
        x, y = point.x(), point.y()
        if authid_norm in ("EPSG:4326", "OGC:CRS84"):
            lon, lat = x, y
        elif authid_norm == "EPSG:3857":
            lon, lat = aoi.merc_to_lonlat(x, y)
        else:
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
                transformed = transform.transform(point)
                lon, lat = transformed.x(), transformed.y()
            except Exception:  # noqa: BLE001 -- honest None, noted on click
                return None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return None
        return lon, lat

    def _toggle_probe_tool(self, checked: bool) -> None:
        """Install/restore the canvas map tool for the Probe button.

        ON: saves the canvas' CURRENT tool (whatever it was -- pan, another
        plugin's tool, ...) then installs a ``QgsMapToolEmitPoint``, built
        once and reused. OFF: restores the saved tool -- the canvas is never
        left on a tool the user did not ask for."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- no canvas (headless) -- no-op
            return
        if checked:
            if self._probe_map_tool is None:
                from qgis.gui import QgsMapToolEmitPoint

                self._probe_map_tool = QgsMapToolEmitPoint(canvas)
                self._probe_map_tool.canvasClicked.connect(
                    self._on_probe_canvas_clicked
                )
            self._prev_map_tool = canvas.mapTool()
            canvas.setMapTool(self._probe_map_tool)
        else:
            if canvas.mapTool() is self._probe_map_tool:
                canvas.setMapTool(self._prev_map_tool)
            self._prev_map_tool = None

    def _toggle_probe_results(self) -> None:
        self.probe_result_label.setVisible(
            self.probe_results_toggle.isChecked()
        )

    def _set_probe_output(self, text: str, error: bool = False) -> None:
        """BUG 3b (live-feedback 2026-07-12): the latest probe status /
        result / error goes to the pinned panel under the message list,
        replaced in place on each click -- NEVER a chat note."""
        self._probe_panel.setVisible(True)
        self.probe_result_label.setText(text)
        self.probe_result_label.setStyleSheet(
            _PROBE_ERROR_BLOCK_STYLE if error else _THINKING_BLOCK_STYLE
        )
        self.probe_result_label.setVisible(
            self.probe_results_toggle.isChecked()
        )

    def _on_probe_canvas_clicked(self, point, _button) -> None:
        if not self._case_id:
            self._set_probe_output(
                "No active case -- open or start a case first.", error=True
            )
            return
        try:
            canvas = self.iface.mapCanvas()
            authid = canvas.mapSettings().destinationCrs().authid()
        except Exception:  # noqa: BLE001 -- no canvas (headless)
            self._set_probe_output(
                "Probe failed: no map canvas available.", error=True
            )
            return
        lonlat = self._point_to_lonlat4326(point, authid)
        if lonlat is None:
            self._set_probe_output(
                "Probe failed: could not transform the clicked point to "
                "EPSG:4326.",
                error=True,
            )
            return
        lon, lat = lonlat
        base_url = (
            self.settings.export_api
            if self.settings.mode == MODE_LOCAL
            else case_export.ws_url_to_http_base(self.settings.remote_url)
        )
        self._set_probe_output(
            f"Probing {probe.probe_location_label(lon, lat)} ..."
        )
        task = _ProbePointTask(base_url, self._case_id, lon, lat, parent=self)
        task.finished.connect(self._on_probe_finished)
        task.errored.connect(self._on_probe_errored)
        self._probe_tasks.append(task)
        task.start()

    def _on_probe_finished(self, lon: float, lat: float, result: dict) -> None:
        lines = probe.format_probe_result(result)
        header = f"Probe {probe.probe_location_label(lon, lat)}:"
        self._set_probe_output("\n".join([header] + [f"  {ln}" for ln in lines]))

    def _on_probe_errored(self, lon: float, lat: float, message: str) -> None:
        label = probe.probe_location_label(lon, lat)
        self._set_probe_output(f"Probe {label} failed: {message}", error=True)

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
            # Live-feedback 2026-07-09: in local mode REUSE the resumed /
            # newest existing case instead of minting a fresh "QGIS session
            # ..." case on every connect (with auto-connect that regrew case
            # clutter per dock-show); create only when zero cases exist.
            # Remote keeps the milestone 1 always-create behavior.
            reuse_case=self.settings.mode == MODE_LOCAL,
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
        # Item 4: the AOI toggles now live only in Settings -- refresh the
        # dock's read-only status line from whatever landed (Save or
        # Cancel; re-reading unchanged settings on Cancel is harmless).
        self._refresh_aoi_status()

    def _open_cases(self) -> None:
        dlg = CasesDialog(self, self._cases)
        self._cases_dialog = dlg
        # Item b/c (live-feedback 2026-07-09): populate from the cold HTTP
        # route when there is nothing to show yet, or the live WS case-list
        # never arrived (not connected) -- so the dialog is never an honest-
        # looking-but-wrong empty state while the agent box actually has
        # cases sitting in Persistence.
        if not self._cases or not self._connected:
            self._load_cold_case_list(dlg)
        try:
            dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        finally:
            self._cases_dialog = None

    def _load_cold_case_list(self, dlg: "CasesDialog") -> None:
        """Fetch ``GET /api/case-list`` off the UI thread and feed the
        result into ``dlg`` (item b/c). Local-mode only in practice (the
        route is 404 outside the local single-user seam -- see
        ``services/agent`` ``tool_catalog_http.py``); remote mode simply
        gets an honest failure note, which is fine since remote already has
        its own Connect-first flow."""
        dlg.info_lbl.setText("Loading cases ...")
        base_url = self.settings.export_api
        task = _CaseListTask(base_url, self)
        task.finished.connect(self._on_cold_case_list_finished)
        task.errored.connect(self._on_cold_case_list_errored)
        self._case_list_tasks.append(task)
        task.start()

    def _on_cold_case_list_finished(self, cases: List[CaseInfo]) -> None:
        # A live WS case-list may have landed while the cold fetch was in
        # flight -- that is authoritative, never clobber it.
        if not self._cases:
            self._cases = cases
        if self._cases_dialog is not None:
            self._cases_dialog.set_cases(self._cases)

    def _on_cold_case_list_errored(self, message: str) -> None:
        if self._cases_dialog is not None and not self._cases:
            self._cases_dialog.info_lbl.setText(
                "Agent HTTP API unreachable - is the local stack running?"
            )

    # -- case switching / new / delete (milestone 3 + item 2/3) ---------------- #

    def open_case(self, case_id: str, title: str) -> None:
        """Open ``case_id`` from the Cases dialog (single click / double
        click on a row, cold-listed or not; item d, live-feedback
        2026-07-09).

        Already connected -> a direct ``select_case`` (works even with no
        active case bound, e.g. right after deleting one). Otherwise (cold
        list, or a connect is still mid-handshake) -> queue the open and
        (auto-)connect; ``_on_case_ready`` performs the deferred select once
        the handshake actually completes, so the click is never silently
        dropped."""
        if self.bridge.running and self._connected:
            self.select_case(case_id, title)
            return
        self._pending_open_case = (case_id, title)
        if not self.bridge.running:
            self._note(f"Connecting to open case '{title}' ...")
            self.connect_agent()
        else:
            self._note(f"Waiting for connection to open case '{title}' ...")

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

    def new_case(self) -> None:
        """Start a fresh case (case-command create) -- header "New" button
        and the Cases dialog's "New case" button both call this. The
        server's case-open reply rebinds the dock (fresh layer group,
        header title, basemap + zoom) through ``_on_case_open_event``, the
        same path a case SELECT rebinds through.
        """
        if not self.bridge.running:
            self.status_label.setText("Not connected -- press Connect first")
            return
        self._note("Starting a new case ...")
        self.bridge.case_command("create")

    def delete_case(self, case_id: str, title: str) -> None:
        """Delete a case (case-command delete). The server re-emits
        case-list, which refreshes the open Cases dialog via ``set_cases``.
        If the deleted case was the dock's active one, clear the case label
        gracefully -- the connection itself stays up."""
        if not self.bridge.running:
            self.status_label.setText("Not connected -- press Connect first")
            return
        self._note(f"Deleting case '{title}' ...")
        self.bridge.case_command("delete", case_id)
        if case_id == self._case_id:
            self._case_id = None
            self._case_title = ""
            self.case_label.setText("No case")
            self.case_label.setVisible(True)

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
        task = _ExportTask(
            base_url, case_id, self, remote=remote, minio_endpoint=self.settings.minio_endpoint
        )
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
        if self.settings.auto_basemap:
            note = ensure_basemap()
            if note:
                self._note(note)
        # Item 1: zoom to the union of the just-exported layers' REAL extents
        # (GeoTIFF/gpkg layers carry true GDAL/OGR extents, unlike the live
        # XYZ tile layers) so the canvas is never left white/stale.
        try:
            canvas = self.iface.mapCanvas()
            dest_crs = canvas.mapSettings().destinationCrs()
            extent = self.materializer.last_added_export_extent(dest_crs)
            zoom_to_extent(canvas, extent)
        except Exception:  # noqa: BLE001 -- headless/no canvas, skip the zoom
            pass
        self._scroll_to_bottom()

    def _on_export_errored(self, case_id: str, message: str) -> None:
        if "has no layers to export" in message:
            # Friendly, not an error: an empty case is expected, not broken.
            self._note(
                "This case has no layers yet -- open it and run a prompt to "
                "generate data.",
                error=False,
            )
            return
        self._note(f"Case export failed: {message}", error=True)

    # -- push active layer into the case -------------------------------------- #

    def _push_active_layer(self) -> None:
        """Header "Push layer" button: send ``iface.activeLayer()`` into the
        current case as a first-class input layer.

        Ask-free UX: a single click, ONE tiny confirm popover (the "Set as
        case AOI" checkbox -- default off -- since that mutates the case
        extent and deserves a beat of confirmation; everything else proceeds
        without further prompting), then one worker-thread round trip
        (export to a temp file, upload, register).
        """
        if not self._case_id:
            self._note(
                "No active case -- open or start a case first.", error=True
            )
            return
        layer = self.iface.activeLayer()
        if layer is None:
            self._note(
                "No active layer -- select a layer in the Layers panel first.",
                error=True,
            )
            return

        box = QMessageBox(self)
        box.setWindowTitle("Push layer")
        box.setText(f"Send '{layer.name()}' to the current case?")
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Ok)
        aoi_checkbox = QCheckBox("Set as case AOI")
        aoi_checkbox.setChecked(False)
        box.setCheckBox(aoi_checkbox)
        if box.exec_() != QMessageBox.Ok:
            return
        make_aoi = aoi_checkbox.isChecked()

        base_url = (
            self.settings.export_api
            if self.settings.mode == MODE_LOCAL
            else case_export.ws_url_to_http_base(self.settings.remote_url)
        )
        self._note(f"Pushing '{layer.name()}' to the case ...")
        task = _PushLayerTask(
            base_url, self._case_id, layer, make_aoi=make_aoi, parent=self
        )
        task.finished.connect(self._on_push_layer_finished)
        task.errored.connect(self._on_push_layer_errored)
        self._push_tasks.append(task)
        task.start()

    def _on_push_layer_finished(self, layer_name: str, result: dict) -> None:
        display_name = layer_name or result.get("name") or "Layer"
        self._note(push_layer.format_push_note(display_name, result))
        # Repaint: re-select the current case so the server replays the
        # fresh layer list (design: "the layer appears via the normal
        # replay/session-state path, or trigger a case re-open to repaint").
        if self.bridge.running and self._case_id:
            self.select_case(self._case_id, self._case_title or self._case_id[:8])
        self._scroll_to_bottom()

    def _on_push_layer_errored(self, layer_name: str, message: str) -> None:
        label = f"'{layer_name}'" if layer_name else "Push layer"
        self._note(f"{label} failed: {message}", error=True)

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
        # Item d (live-feedback 2026-07-09): a case picked from the Cases
        # dialog while disconnected/mid-handshake -- the connection just
        # created its own fresh "QGIS session ..." case; now switch to the
        # one the user actually asked for.
        if self._pending_open_case is not None:
            pending_id, pending_title = self._pending_open_case
            self._pending_open_case = None
            if pending_id != case_id:
                self.select_case(pending_id, pending_title)

    def _on_failed(self, message: str) -> None:
        self._connected = False
        self._pending_open_case = None  # the connect this was riding died
        self._set_dot("error")
        self.status_label.setText(f"Connection failed: {message}")
        self.connect_btn.setText("Connect")

    def _on_auth_expired(self, message: str) -> None:
        """The token was rejected (broker 401/403 or in-band AUTH_REQUIRED):
        the worker has STOPPED -- no silent reconnect loop. Say exactly what
        to do next."""
        self._connected = False
        self._pending_open_case = None  # the connect this was riding died
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
                    # Compaction UX (Part A): "context:compact" is the one
                    # step whose tool_name is a plain internal id
                    # ("context:compact") while step.name carries the actual
                    # human-readable state -- "Compacting conversation..."
                    # then "Conversation compacted (Nk -> Mk tokens)". Every
                    # other tool's tool_name IS already the readable label
                    # (or at least as readable as step.name), so this stays
                    # scoped to the one case where preferring name is
                    # strictly better, never changing existing behavior.
                    label = (
                        step.name
                        if step.tool_name == "context:compact"
                        else (step.tool_name or step.name)
                    )
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
                    # BUG 3a (live-feedback 2026-07-12): one collapsed
                    # "Layers (N)" toggle per batch, not N chat lines
                    # (errors stay visible outside the collapse).
                    self._ensure_pending().add_layer_notes(notes)
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
        """A ``case-open`` rehydration arrived -- the select response, AND
        (as of item 2/3) a mid-session ``case_command("create")`` reply too,
        since that now sends without blocking on the reply (only the
        INITIAL connect's create_case still consumes its case-open inside
        the worker handshake, via ``_on_case_ready``).

        Rebinds the dock: authoritative title in the header, a FRESH layer
        group named for the case (dedup reset), the persisted loaded_layers
        replayed into it, an OpenStreetMap basemap (settings-gated, item 4),
        and a canvas zoom to the case bbox -- or, absent one, the union of
        the vector layers just materialized, or a further fallback bbox --
        so the canvas is never silently left wherever it was (item 1,
        live-feedback 2026-07-09; the fallback ladder + honest note for a
        genuinely bbox-less raster-only case is item D, live-feedback
        2026-07-10). Runs LAST, unconditionally, on every case-open path
        that reaches this handler (select, New case, and the startup-reuse
        select alike -- ``_bind_startup_case`` also resolves through a
        ``case-command select`` whose reply lands here) -- nothing above
        this call may skip or short-circuit past it.
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
        # ITEM B: the previous case's bubbles/notes/gate cards must not
        # survive a switch -- clear the message list before repainting
        # anything for the newly-opened case.
        self._clear_messages()
        self.materializer.set_case(info.case_id, info.title)
        self._set_case_label(info.title)
        self._set_dot("connected")
        self.status_label.setText(f"Connected -- case {info.case_id[:8]}")
        self._replay_chat_history(info.chat_messages)
        self._note(f"Case '{info.title}' active")
        if info.layers:
            notes = self.materializer.materialize(info.layers)
            if notes:
                # BUG 3a (live-feedback 2026-07-12): the case-open replay
                # used to paint one chat line PER layer (21 on a real case),
                # pushing the conversation far up -- fold the batch into the
                # collapsed "Layers (N)" toggle (errors stay visible).
                self._ensure_pending().add_layer_notes(notes)
        if self.settings.auto_basemap:
            note = ensure_basemap()
            if note:
                self._note(note)
        self._zoom_after_case_open(info)
        self._scroll_to_bottom()

    def _zoom_after_case_open(self, info) -> None:
        """ITEM D (live-feedback 2026-07-10, auto-focus on every case
        switch): zoom the canvas to the just-opened case's area, and say
        so. Fallback ladder, each rung tried only when the previous one is
        absent or its transform fails:

          1. ``info.bbox`` -- the case-open's own ``session_state.case.bbox``
             (EPSG:4326).
          2. the union of the vector layers THIS case-open just
             materialized (XYZ raster layers report a whole-world extent,
             so they never drive this fallback).
          3. any bbox found elsewhere in the case-open's raw payload
             (``find_fallback_bbox`` -- defensive/future-proof; today's
             wire contract carries no OTHER bbox field).
          4. the dock's own CACHED ``case-list`` row for this case_id
             (``CaseInfo.bbox``, populated independently of the case-open
             rehydration) -- can carry a bbox even when (1)-(3) come up
             empty.

        A successful zoom appends "Zoomed to case area" so the behavior is
        visible. A genuinely bbox-less, vector-less case (an OLD raster-only
        case predating bbox seeding) says so honestly instead of silently
        leaving the view wherever it was. Headless-safe: no canvas (no
        ``iface``) is a silent no-op, never a crash (there would be no one
        to show the note to in that environment either).
        """
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- no canvas (headless), nothing to zoom
            return
        if info.bbox is not None and zoom_to_bbox4326(canvas, info.bbox):
            self._note("Zoomed to case area")
            return
        try:
            dest_crs = canvas.mapSettings().destinationCrs()
        except Exception:  # noqa: BLE001
            return
        extent = self.materializer.last_added_vector_extent(dest_crs)
        if zoom_to_extent(canvas, extent):
            self._note("Zoomed to case area")
            return
        fallback = self._fallback_case_bbox(info)
        if fallback is not None and zoom_to_bbox4326(canvas, fallback):
            self._note("Zoomed to case area")
            return
        self._note("Case has no stored map area - keeping current view")

    def _fallback_case_bbox(
        self, info
    ) -> Optional[Tuple[float, float, float, float]]:
        """ITEM D rungs 3-4: a bbox from OUTSIDE ``info.bbox`` -- the
        case-open's own raw payload, then the dock's cached ``case-list``
        row for this case_id. Returns the first usable EPSG:4326 bbox, or
        None. Never raises -- a malformed cached row is skipped, not fatal.
        """
        raw_bbox = find_fallback_bbox(info.raw)
        if raw_bbox is not None:
            return raw_bbox
        for case in self._cases:
            try:
                if case.case_id != info.case_id or not case.bbox:
                    continue
                bbox = case.bbox
                if len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
                    return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            except (AttributeError, TypeError, ValueError):
                continue
        return None

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
        # BUG 4 (live-feedback 2026-07-12, NATE: the card sat "at the bottom
        # and the response chat is above"): the streaming _AssistantEntry
        # was created BEFORE the card, so everything after the user's
        # confirm streamed into the entry ABOVE it. Close out the pending
        # entry here -- the next event (thinking/chunk/pipeline/note) mints
        # a FRESH entry via _ensure_pending, which inserts before the
        # terminal stretch, i.e. BELOW the card -- the cloud web's
        # chronological turn flow.
        self._pending = None
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
