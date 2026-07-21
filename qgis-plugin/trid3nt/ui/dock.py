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
import json
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# Item R6 (live-feedback 2026-07-18): layer-type constant for registering the
# "Push layer to case" layer-tree context-menu action (vector + raster).
from qgis.core import QgsMapLayer

from . import charts, gate
from .cards import (
    GateCard,
    SimCard,
    _AssistantEntry,
    _ChatInput,
    _ToolCard,
    _WrapLabel,
)
from .cases_dialog import CasesDialog
from .settings_dialog import SettingsDialog
from ._style import (
    _PROBE_ERROR_BLOCK_STYLE,
    _THINKING_BLOCK_STYLE,
    _THINKING_TOGGLE_STYLE,
)
from ..case import aoi, case_export, push_layer
from ..net.tasks import (
    _CaseListTask,
    _EffectiveModelTask,
    _ExportTask,
    _ProbePointTask,
    _ProviderConfigTask,
    _PushLayerTask,
)
from ..net.trid3nt_client import (
    CaseInfo,
    Debouncer,
    PipelineStep,
    find_fallback_bbox,
    parse_case_open,
)
from ..net.ws_bridge import AgentBridge
from ..plugin_settings import MODE_LOCAL, PluginSettings
from ..render import probe
from ..render.layers import (
    LayerMaterializer,
    ensure_basemap,
    zoom_to_bbox4326,
    zoom_to_extent,
)



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
# H1 (NATE 2026-07-20): _DOT_TITLES removed -- the titlebar no longer carries
# the connection STATE (the coloured dot alone signifies it). The titlebar
# shows the active CASE NAME (else "TRID3NT"); see ``_set_case_label``.

_USER_BUBBLE_STYLE = (
    "background-color: #1f6feb; color: white; border-radius: 8px; padding: 6px 9px;"
)


def _solver_engine_label(solver: str) -> str:
    """Item R4 (live-feedback 2026-07-18): "telemac_river_dye" or
    "telemac_river_dye:solve" -> "TELEMAC" -- the SimCard title names the
    ENGINE; the full run identity stays visible in the metadata table.
    Pure string math on the emitter's own naming convention
    (``mint_dispatch_and_sim_cards`` stamps ``tool_name="<solver>:solve"``,
    solver ids lead with the engine: sfincs_* / modflow_* / telemac_*)."""
    base = (solver or "").split(":", 1)[0]
    head = base.split("_", 1)[0]
    return head.upper() if head else "SOLVER"


def _short_args_summary(raw_args: str, max_len: int = 64) -> str:
    """Item R2 (live-feedback 2026-07-18): a compact ``k=v, k=v`` arg summary
    for the tool chip row, from the ``tool-io`` sidecar's pre-serialized
    ``raw_args`` JSON string (contract ws.ToolIoPayload). First 3 keys only,
    each value clipped; nested structures collapse to "..." (the chip is a
    summary, not an IO dump). Defensive: non-JSON / non-dict / empty input
    yields "" -- the chip then renders without args, never a crash."""
    try:
        args = json.loads(raw_args)
    except (ValueError, TypeError):
        return ""
    if not isinstance(args, dict) or not args:
        return ""
    parts: List[str] = []
    for key, value in args.items():
        if isinstance(value, (dict, list)):
            value = "..."
        text = f"{key}={value}"
        if len(text) > 24:
            text = text[:21] + "..."
        parts.append(text)
        if len(parts) >= 3:
            break
    summary = ", ".join(parts)
    if len(summary) > max_len:
        summary = summary[: max_len - 3] + "..."
    return summary


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
        # OpenRouter model-extensibility (design 2026-07-19): the Settings Save
        # provider-config POST tasks -- owned here (not the closing dialog).
        self._provider_config_tasks: List[_ProviderConfigTask] = []
        # The Probe map tool (design point 2): built lazily on first toggle-on
        # (QgsMapToolEmitPoint needs a live canvas). ``_prev_map_tool`` is the
        # canvas' tool saved right before the Probe tool is installed, so
        # toggling off restores it (never steals the tool permanently).
        self._probe_map_tool = None
        self._prev_map_tool = None
        # Persistent per-case bbox (per-case-bbox 2026-07-19, cloud parity):
        # the case AOI the agent references every turn + the user can re-draw.
        # ``_case_bbox`` is the current EPSG:4326 ``(w, s, e, n)`` (None until a
        # case-open carries one / the user draws one); ``_aoi_rubber`` is the
        # dashed outline-only overlay that shows it on the canvas. Both are
        # CLEARED on every case switch (``_clear_messages``) + disconnect
        # (``disconnect_agent``) so a stale box never lingers across a switch.
        # The "Set AOI" tool reuses the release-point pick discipline: ON saves
        # the canvas' current tool + installs ``QgsMapToolExtent``, OFF restores
        # it (``_aoi_map_tool`` / ``_prev_aoi_tool``, mirroring the probe pair).
        self._case_bbox: Optional[Tuple[float, float, float, float]] = None
        self._aoi_rubber = None
        self._aoi_map_tool = None
        self._prev_aoi_tool = None
        # A1 (NATE 2026-07-20): True while the Set-AOI canvas key-filter (BACKSPACE
        # /DELETE -> _clear_aoi) is installed -- see ``_toggle_aoi_draw``.
        self._aoi_key_filter_on = False
        self._refresh_debounce = Debouncer()
        # Item d (live-feedback 2026-07-09): a case picked from the Cases
        # dialog before/while connecting -- opened via ``_on_case_ready``
        # once the (auto-)connect actually completes, so a cold-list click
        # is never silently dropped.
        self._pending_open_case: Optional[Tuple[str, str]] = None
        # AUTO-CONNECT (live-feedback 2026-07-09): fires once per dock SHOW,
        # reset on hide -- see ``showEvent``/``hideEvent``/``_auto_connect_local_once``.
        self._auto_connect_done_this_show = False
        # Item R4 (live-feedback 2026-07-18): live SimCards keyed by the
        # compute step's step_id; reset on case switch (_clear_messages).
        self._sim_cards: Dict[str, SimCard] = {}
        # Item R2 (live-feedback 2026-07-18): short arg summaries from the
        # tool-io sidecar, keyed by step_id, for the tool chip rows.
        self._tool_args_by_step: Dict[str, str] = {}
        # O1 (NATE 2026-07-20): the standing "AOI: drawn X x Y deg" readout is
        # GONE (clutter). The ONLY AOI note is the one-time inline transcript
        # note ``_on_aoi_extent_chosen`` emits WHEN the user actually sets the
        # AOI ("Case AOI set to ..."). Nothing is restated per-send anymore, so
        # the old ``_aoi_status_line`` / ``_last_aoi_note`` dedupe pair is
        # removed with it.

        self._build_ui()
        self._wire_bridge()
        # Item R6 (live-feedback 2026-07-18): the persistent "Push layer"
        # header button was UI noise (NATE ask) -- the push action now lives
        # in the QGIS layer-tree context menu ("Push layer to case").
        self._push_tree_actions: List = []
        self._register_layer_tree_push_action()

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

        # Row 1 (status strip): the connection dot (left) + the active LLM
        # model name. The dot's COLOUR is the connection signifier (green =
        # connected); the text shows the RUNNING MODEL -- not a redundant
        # "Connected" word (the dot means that) and not a case-id (that lives
        # in the case title). Buttons live on their OWN row below so this stays
        # a pure status strip. NATE 2026-07-20.
        status_row = QHBoxLayout()
        self.dot = QLabel()
        self._set_dot("disconnected")
        status_row.addWidget(self.dot)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size: 9pt;")
        status_row.addWidget(self.status_label, 1)
        outer.addLayout(status_row)

        # The agent's effective model id (settings picker override, else the
        # agent env default probed on connect) shown in the status strip.
        self._effective_model: str = ""
        self._effective_model_tasks: List["_EffectiveModelTask"] = []

        # Row 2 (actions): PURE buttons -- Cases / Probe / Set AOI /
        # Settings(cog). Connect + Disconnect BOTH live in Settings now (the
        # dock auto-connects in local mode; a greyed header Connect button was
        # noise -- NATE 2026-07-20). H3: "New" dropped (creation via the Cases
        # dialog); A1: "Clear AOI" dropped (BACKSPACE clears while Set-AOI is
        # active).
        button_row = QHBoxLayout()
        self.cases_btn = QToolButton()
        self.cases_btn.setText("Cases")
        self.cases_btn.clicked.connect(self._open_cases)
        button_row.addWidget(self.cases_btn)

        # H3 (NATE 2026-07-20): the header "New" case button is REMOVED --
        # case creation stays available in the Cases dialog. The ``new_case()``
        # METHOD remains (the Cases dialog + the auto/startup paths still call
        # it); only the header widget is gone.

        # Item R6 (live-feedback 2026-07-18): the "Push layer" header button
        # is REMOVED (UI-noise reduction, NATE ask). The bidirectional layer
        # push (the reverse seam of "Open in QGIS") lives in the QGIS
        # layer-tree context menu instead -- see
        # ``_register_layer_tree_push_action``; ``_push_active_layer`` is
        # still the backing method.

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
        button_row.addWidget(self.probe_btn)

        # Persistent per-case bbox (per-case-bbox 2026-07-19): drag a rectangle
        # on the map to set THIS case's area of interest -- the extent the
        # agent references every turn (state.case_bbox). Checkable, same
        # discipline as Probe: ON saves whatever tool is active + installs a
        # QgsMapToolExtent; the chosen extent persists via case-command
        # set-bbox and restores the prior tool. Only usable with a live case +
        # connection (guarded in _toggle_aoi_draw).
        self.aoi_btn = QToolButton()
        self.aoi_btn.setText("Set AOI")
        self.aoi_btn.setCheckable(True)
        self.aoi_btn.setToolTip(
            "Drag a rectangle on the map to set this case's area of interest"
        )
        self.aoi_btn.toggled.connect(self._toggle_aoi_draw)
        button_row.addWidget(self.aoi_btn)

        # A1 (NATE 2026-07-20): the "Clear AOI" header button is REMOVED --
        # clearing is MULTIPLEXED into the Set-AOI tool: while Set-AOI is active
        # (aoi_btn checked), pressing BACKSPACE or DELETE clears the current AOI
        # via ``_clear_aoi`` (a canvas eventFilter installed only while the tool
        # is on). ``_clear_aoi`` stays as the backing method.

        # Item B2 (qgis-ux-batch 2026-07-19): Settings collapses to a COG glyph
        # (icon-only look) -- the header button row is pure buttons, no word
        # labels competing with the connection signifier. The gear glyph is the
        # button's only content (effectively icon-only); the tooltip names it.
        button_row.addStretch(1)  # push Settings to the right end of the row
        self.settings_btn = QToolButton()
        self.settings_btn.setText("\u2699")  # gear glyph (cog); icon-only look
        self.settings_btn.setToolTip("Settings (connect / disconnect live here)")
        self.settings_btn.clicked.connect(self._open_settings)
        button_row.addWidget(self.settings_btn)
        outer.addLayout(button_row)

        # H2 (NATE 2026-07-20): the under-button "Case: <title>" line is REMOVED
        # -- the active case name now rides the dock TITLEBAR (``_set_case_label``
        # -> setWindowTitle), freeing this vertical space.

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        outer.addWidget(line)

        # Message list
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        # E1 (NATE 2026-07-20): the transcript NEVER grows a horizontal
        # scrollbar -- error text (and every other chat line) must reflow with
        # the resizable chat panel, not pin it wide. AlwaysOff (was the default
        # ScrollBarAsNeeded, which a long unbroken error token could trip);
        # wrapped labels cap their minimum width to 1 so content reflows.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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

        # OpenQuake result parity (live-feedback 2026-07-13): the Charts
        # panel -- the QGIS twin of the web's chart cards (hazard curves,
        # UHS, damage distributions, ...). Same pinned-collapsible pattern
        # as the probe panel: charts render HERE, never in the chat message
        # list (NATE's clutter rule). Populated from the case-open replay
        # (``session_state.charts``) and live ``chart-emission`` frames;
        # cleared on case switch. Hidden until the case has a chart.
        self.charts_panel = charts.ChartsPanel(
            toggle_style=_THINKING_TOGGLE_STYLE,
            block_style=_THINKING_BLOCK_STYLE,
        )
        outer.addWidget(self.charts_panel)

        # Item 4 (live-feedback 2026-07-09): the AOI toggles (canvas /
        # selected polygon) moved into Settings -- apply-on-Save there now,
        # instead of live-applying from checkboxes here. F9 "Show model
        # thinking" moved into the Settings dialog (NATE live-feedback
        # 2026-07-13) -- the send path keeps reading
        # ``self.settings.show_thinking``.
        # Item R3 (live-feedback 2026-07-18) + O1 (NATE 2026-07-20): NO pinned
        # AOI status line above the composer, AND no per-send restated readout
        # either. The ONLY AOI note is the one-time "Case AOI set to ..." line
        # ``_on_aoi_extent_chosen`` emits when the user actually sets the AOI.

        # Input row -- Item A (qgis-ux-batch 2026-07-19): a multi-line
        # auto-growing composer (_ChatInput) replaces the one-line QLineEdit
        # that clipped long prompts. ENTER sends, SHIFT+ENTER newlines.
        # C1 (NATE 2026-07-20): the Send button is REMOVED -- ENTER already
        # sends, so the composer is the sole full-width widget in the row
        # (``_send`` stays, wired to ENTER via ``_ChatInput``).
        input_row = QHBoxLayout()
        self.input_edit = _ChatInput(self._send)
        self.input_edit.setPlaceholderText("Ask for data or a simulation...")
        input_row.addWidget(self.input_edit, 1)
        outer.addLayout(input_row)

        self.setWidget(body)

    def _set_dot(self, state: str) -> None:
        color = _DOT_COLORS.get(state, _DOT_COLORS["disconnected"])
        self.dot.setStyleSheet(f"background-color: {color}; {_DOT_STYLE}")
        # H1 (NATE 2026-07-20): the titlebar no longer carries the CONNECTION
        # STATE -- the coloured dot alone signifies connection. The titlebar
        # instead shows the active CASE NAME (else "TRID3NT"), driven from the
        # case paths via ``_set_case_label``; nothing to repaint here.

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
        # Item R4/R2 (live-feedback 2026-07-18): per-case transcript state --
        # the SimCard registry (widgets die in the loop below) + the tool-io
        # arg summaries. (O1, NATE 2026-07-20: the per-send AOI note dedupe is
        # gone -- the AOI note now fires only on an explicit Set-AOI.)
        self._sim_cards.clear()
        self._tool_args_by_step.clear()
        # Per-case-bbox 2026-07-19: the previous case's AOI overlay must not
        # linger across a switch -- _on_case_open_event repaints it below from
        # the newly-opened case's own bbox (or leaves it cleared when absent).
        self._clear_aoi_overlay()
        # BUG 3b (live-feedback 2026-07-12): the probe panel shows CASE
        # data -- a table from the previous case must not linger across a
        # switch. Hide it (its next click repopulates it).
        self._probe_panel.setVisible(False)
        self.probe_result_label.setText("")
        # Charts are per-Case state too (live-feedback 2026-07-13): the
        # case-open replay below repopulates the panel for the new case.
        self.charts_panel.clear()
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _replay_chat_history(self, messages: List[dict]) -> None:
        """ITEM B / T9 (NATE 2026-07-20): repaint a just-opened case's persisted
        conversation before the "Case '<title>' active" note.

        T9 unifies the replay path with the live path: a run of consecutive
        persisted ``role="tool"`` rows is grouped into ONE parent ``_ToolCard``
        (the SAME widget the live pipeline builds), with each tool's RESULT
        shown under it INSIDE the card -- so a reopened case shows the identical
        parent format instead of a stack of separate flat cards. E2: a persisted
        tool FAILURE (``is_error``) survives the reopen as a red row + result
        inside that card. Any persisted THINKING (defensive -- read off the row
        if a future server carries it) replays as the collapsed thinking block
        on the agent entry (NATE lost both the tool chain + thinking on refresh).

        ``messages`` is already role/content-filtered + capped by
        ``trid3nt_client.parse_chat_history``; an empty list is a no-op."""
        tool_group: List[dict] = []
        for row in messages:
            role = row.get("role")
            if role == "tool":
                # Accumulate the tool run -- flushed into ONE card when the run
                # ends (next non-tool row, or end of history).
                card = self._resolve_tool_card(
                    row.get("tool_card"), row.get("content")
                )
                if card is not None:
                    tool_group.append(card)
                continue
            # A non-tool row closes any open tool run.
            if tool_group:
                self._replay_tool_group(tool_group)
                tool_group = []
            content = row.get("content")
            # E2: a persisted terminal-error row (defensive -- a future server
            # role="error", or an agent row flagged is_error) replays as a
            # wrapped inline error line, same place it appeared live.
            if role == "error" or row.get("is_error"):
                if isinstance(content, str) and content:
                    self._note(content, error=True)
                continue
            if not isinstance(content, str) or not content:
                continue
            if role == "user":
                self._add_user_bubble(content)
            elif role == "agent":
                entry = _AssistantEntry(self.messages_layout)
                # T9: replay any persisted reasoning as the collapsed thinking
                # block (defensive -- parse_chat_history drops it today, so this
                # only fires once the server persists thinking on the row).
                thinking = row.get("thinking") or row.get("reasoning")
                if isinstance(thinking, str) and thinking.strip():
                    entry.append_thinking_delta(thinking)
                    entry.collapse_thinking()
                entry.append_delta(content)
                # Feature 2026-07-13: replayed text is always final --
                # render its markdown immediately.
                entry.finalize_markdown()
        if tool_group:
            self._replay_tool_group(tool_group)

    @staticmethod
    def _resolve_tool_card(tool_card, content) -> Optional[dict]:
        """Resolve ONE persisted tool row to its ``ToolCardRecord`` dict
        (contracts ``case.py``): the typed ``tool_card`` when present, else the
        ``content`` JSON twin. Malformed/empty -> None (skipped, never raises)."""
        card = tool_card if isinstance(tool_card, dict) else None
        if card is None and isinstance(content, str) and content:
            try:
                parsed = json.loads(content)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                card = parsed
        return card or None

    def _replay_tool_group(self, cards: List[dict]) -> None:
        """T9: render a run of persisted tool rows as ONE parent ``_ToolCard``
        -- the SAME builder the live pipeline uses (``_ToolCard.set_content``).

        Each card becomes an inner row (``tool_name``/``label`` + ``state``)
        with its ``function_response`` shown under it as a collapsed read-only
        body INSIDE the card; ``is_error`` rows render as a failed row (x glyph)
        with the red result block. The parent card is all-terminal on replay, so
        ``set_content`` auto-collapses it (matching the live end-state, T3); the
        chevron re-expands it. The bottom metadata block (T7) carries each
        tool's short arg summary."""
        inner_rows: List[dict] = []
        meta_lines: List[str] = []
        for card in cards:
            name = card.get("tool_name") or card.get("label") or "tool"
            state = card.get("state")
            is_error = bool(card.get("is_error"))
            raw_args = card.get("raw_args")
            response = card.get("function_response")
            # E2: an error card shows the failed (x) glyph regardless of the
            # raw state word.
            row_state = "failed" if is_error else state
            inner_rows.append(
                {"label": str(name),
                 "state": row_state,
                 "nested": False,
                 "result": response if isinstance(response, str) else None,
                 "is_error": is_error}
            )
            args_summary = (
                _short_args_summary(raw_args) if isinstance(raw_args, str) else ""
            )
            if args_summary:
                meta_lines.append(f"{name}: {args_summary}")
        if not inner_rows:
            return
        tool_card = _ToolCard()
        tool_card.set_content(inner_rows, meta_lines)
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1, tool_card
        )

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
        """The ``(bbox, source)`` to attach right now, or ``(None, None)``.

        A2 (NATE 2026-07-20): the AOI model is now EXPLICIT-only -- the user
        DREW an AOI (the Set-AOI rectangle, persisted as ``self._case_bbox``,
        which the rehydrated case bbox also populates) OR no AOI is set and the
        AGENT geocodes the location from the message. The canvas-as-AOI and
        selection-polygon paths are GONE (no reads of ``settings.canvas_aoi`` /
        ``settings.selection_aoi``). Returns the drawn/persisted case bbox when
        one exists and is within the size guard, else ``(None, None)``.

        O1 (NATE 2026-07-20): this NO LONGER stamps a standing status line --
        the AOI is noted once at set time (``_on_aoi_extent_chosen``), never
        restated per send.
        """
        bbox = self._case_bbox
        if bbox is not None and aoi.bbox_within_guard(bbox):
            return bbox, "drawn"
        return None, None

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

    # -- case AOI (persistent per-case bbox, per-case-bbox 2026-07-19) --------- #

    def _render_aoi_overlay(
        self, bbox4326: Tuple[float, float, float, float]
    ) -> None:
        """Paint (or repaint) the case AOI as a DASHED, outline-only rectangle
        on the canvas -- the QGIS twin of the web's analysis-extent overlay.

        The bbox is EPSG:4326 ``(w, s, e, n)``; the canvas may be 3857/other,
        so the ring is transformed 4326 -> canvas CRS via QgsCoordinateTransform
        exactly like ``layers.zoom_to_bbox4326`` (never hand-rolled). A single
        reused ``QgsRubberBand`` (built lazily -- it needs a live canvas) holds
        the geometry; a subtle blue accent, Qt.DotLine pen, transparent fill so
        it reads as an EXTENT, not a filled feature. Headless-safe: no canvas
        (or any construction failure) is a silent no-op, never a crash -- the
        state (``_case_bbox``) is still authoritative for the agent.
        """
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no iface -- no overlay
            return
        try:
            from qgis.core import (
                QgsCoordinateReferenceSystem,
                QgsCoordinateTransform,
                QgsGeometry,
                QgsProject,
                QgsRectangle,
                QgsWkbTypes,
            )
            from qgis.gui import QgsRubberBand
            from qgis.PyQt.QtGui import QColor

            lon_min, lat_min, lon_max, lat_max = bbox4326
            rect = QgsRectangle(lon_min, lat_min, lon_max, lat_max)
            dst_crs = canvas.mapSettings().destinationCrs()
            src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            if src_crs != dst_crs:
                transform = QgsCoordinateTransform(
                    src_crs, dst_crs, QgsProject.instance().transformContext()
                )
                rect = transform.transformBoundingBox(rect)
            if self._aoi_rubber is None:
                self._aoi_rubber = QgsRubberBand(
                    canvas, QgsWkbTypes.PolygonGeometry
                )
                accent = QColor("#58a6ff")
                self._aoi_rubber.setColor(accent)
                self._aoi_rubber.setWidth(2)
                # Outline-only: a fully transparent fill leaves just the ring.
                self._aoi_rubber.setFillColor(QColor(0, 0, 0, 0))
                try:
                    self._aoi_rubber.setLineStyle(Qt.DotLine)
                except Exception:  # noqa: BLE001 -- older builds lack it
                    pass
            self._aoi_rubber.setToGeometry(QgsGeometry.fromRect(rect), None)
            self._aoi_rubber.show()
        except Exception:  # noqa: BLE001 -- overlay is cosmetic; never crash
            return

    def _clear_aoi_overlay(self) -> None:
        """Drop the case AOI state + hide the overlay -- called on every case
        switch (``_clear_messages``) and disconnect (``disconnect_agent``) so a
        stale box from the previous case never lingers on the canvas."""
        self._case_bbox = None
        if self._aoi_rubber is None:
            return
        try:
            from qgis.core import QgsWkbTypes

            self._aoi_rubber.reset(QgsWkbTypes.PolygonGeometry)
            self._aoi_rubber.hide()
        except Exception:  # noqa: BLE001 -- best-effort teardown
            pass

    def _toggle_aoi_draw(self, checked: bool) -> None:
        """Install/restore the canvas map tool for the "Set AOI" button --
        the exact release-point / probe discipline: ON saves whatever tool is
        active then installs a ``QgsMapToolExtent`` (its ``extentChanged``
        signal is the box analog of the point tool's ``canvasClicked``); OFF
        restores the saved tool so the canvas is never left on a tool the user
        did not ask for. Guarded: only usable with a live case + connection
        (mirrors the probe-click guard) -- without one the button snaps back
        off with an honest status line."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no canvas -- no-op
            return
        if checked:
            if not (self._case_id and self.bridge.running):
                self.status_label.setText(
                    "Not connected -- open a case first to set its AOI"
                )
                # Snap back off WITHOUT re-entering this slot (blockSignals),
                # so the guard cannot recurse through the toggled signal.
                self.aoi_btn.blockSignals(True)
                self.aoi_btn.setChecked(False)
                self.aoi_btn.blockSignals(False)
                return
            if self._aoi_map_tool is None:
                try:
                    from qgis.gui import QgsMapToolExtent

                    self._aoi_map_tool = QgsMapToolExtent(canvas)
                    self._aoi_map_tool.extentChanged.connect(
                        self._on_aoi_extent_chosen
                    )
                except Exception:  # noqa: BLE001 -- older build lacks the tool
                    # Honest degradation (a press/drag/release rubber-band
                    # fallback is deferred): snap off + say so, never a crash.
                    self._note(
                        "Set AOI is unavailable in this QGIS build "
                        "(QgsMapToolExtent missing).",
                        error=True,
                    )
                    self.aoi_btn.blockSignals(True)
                    self.aoi_btn.setChecked(False)
                    self.aoi_btn.blockSignals(False)
                    return
            self._prev_aoi_tool = canvas.mapTool()
            canvas.setMapTool(self._aoi_map_tool)
            # A1 (NATE 2026-07-20): while Set-AOI is ON, BACKSPACE/DELETE clears
            # the current AOI. A canvas eventFilter (installed here, removed in
            # the OFF branch) catches those keys and routes to _clear_aoi -- the
            # clearing is multiplexed into the Set-AOI tool (no separate button).
            canvas.installEventFilter(self)
            self._aoi_key_filter_on = True
        else:
            if canvas.mapTool() is self._aoi_map_tool:
                canvas.setMapTool(self._prev_aoi_tool)
            self._prev_aoi_tool = None
            if getattr(self, "_aoi_key_filter_on", False):
                canvas.removeEventFilter(self)
                self._aoi_key_filter_on = False

    def eventFilter(self, obj, event):  # noqa: N802 -- Qt-mandated name
        """A1 (NATE 2026-07-20): while the Set-AOI tool is active, BACKSPACE or
        DELETE on the canvas clears the current AOI (``_clear_aoi``). The filter
        is installed only while ``aoi_btn`` is checked (see ``_toggle_aoi_draw``);
        guard on that flag + key so nothing else on the canvas is intercepted,
        and always fall through to the base filter for every other event."""
        from qgis.PyQt.QtCore import QEvent

        if (
            getattr(self, "_aoi_key_filter_on", False)
            and self.aoi_btn.isChecked()
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Backspace, Qt.Key_Delete)
        ):
            self._clear_aoi()
            return True
        return super().eventFilter(obj, event)

    def _on_aoi_extent_chosen(self, rect) -> None:
        """A rectangle was dragged with the "Set AOI" tool: convert the
        canvas-CRS ``QgsRectangle`` -> EPSG:4326 (via ``_rect_to_bbox4326``,
        the same CRS path the canvas/selection AOI uses), update the state,
        repaint the overlay, persist it (case-command ``set-bbox`` -> the
        server sets ``state.case_bbox`` so the pin/snap paths fire every
        turn), then restore the prior map tool + uncheck the button."""
        try:
            canvas = self.iface.mapCanvas()
            authid = canvas.mapSettings().destinationCrs().authid()
        except Exception:  # noqa: BLE001 -- headless / no canvas
            return
        if rect is None or rect.isEmpty():
            return
        bbox = self._rect_to_bbox4326(rect, authid)
        if bbox is None:
            self._note(
                "Could not set AOI: the drawn extent did not resolve to "
                "EPSG:4326.",
                error=True,
            )
            self.aoi_btn.setChecked(False)  # restores the prior tool
            return
        self._case_bbox = bbox
        self._render_aoi_overlay(bbox)
        if self._case_id and self.bridge.running:
            self.bridge.case_command(
                "set-bbox", self._case_id, {"bbox": list(bbox)}
            )
            self._note(f"Case AOI set to {aoi.format_bbox(bbox)}")
        # Restore the prior map tool + pop the button (setChecked(False) runs
        # _toggle_aoi_draw's OFF branch, which restores canvas.mapTool()).
        self.aoi_btn.setChecked(False)

    def _clear_aoi(self) -> None:
        """Item D (qgis-ux-batch 2026-07-19): clear the current case AOI.

        Drops the local overlay + nulls ``self._case_bbox`` (``_clear_aoi_overlay``)
        AND resets ``state.case_bbox`` server-side so the agent stops anchoring
        every turn on the old extent -- the clear equivalent of the Set-AOI
        ``set-bbox`` send (an empty ``bbox`` = CLEAR; the server treats an
        explicit null/empty bbox as a reset). Honest note either way; with no
        live case + connection there is nothing to sync, so it only clears the
        local overlay and says so.
        """
        had = self._case_bbox is not None
        self._clear_aoi_overlay()  # hides overlay + nulls self._case_bbox
        if self._case_id and self.bridge.running:
            # Mirror the Set-AOI send with an empty bbox (the CLEAR carrier).
            self.bridge.case_command("set-bbox", self._case_id, {"bbox": None})
            self._note("Case AOI cleared" if had else "No AOI was set")
        else:
            self._note(
                "AOI overlay cleared (not connected -- nothing to sync)"
            )

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
        # Item B3 (qgis-ux-batch 2026-07-19): the top-row button only CONNECTS;
        # disable it while a connection is up/in-flight (Disconnect lives in
        # Settings now). Re-enabled by disconnect_agent + the failure paths.
        title = "QGIS session " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._session_case_title = title
        anon = self.settings.anonymous_user_id or None
        # A2 (NATE 2026-07-20): a fresh case is BBOX-LESS -- never seed the
        # canvas extent on create. The AOI is set explicitly later (the Set-AOI
        # rectangle) or the agent geocodes it; there is no canvas-as-AOI path.
        self.bridge.start(
            url,
            token=self.settings.effective_token(),
            anonymous_user_id=anon if self.settings.mode == MODE_LOCAL else None,
            case_title=title,
            case_bbox=None,
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
        # Per-case-bbox 2026-07-19: the case AOI is per-case state -- a
        # disconnect ends the case binding, so the overlay must go too.
        self._clear_aoi_overlay()
        self._set_case_label("")
        self._set_dot("disconnected")
        self.status_label.setText("Not connected")
        # Item B3: re-arm the top-row Connect button (disabled while connected).

    def _set_case_label(self, title: str) -> None:
        # H1/H2 (NATE 2026-07-20): the case name moved to the dock TITLEBAR --
        # the under-button "Case: <title>" line (self.case_label) is gone. One
        # method both the case-open and no-case paths call: a title = that case
        # in the titlebar; empty = the "TRID3NT" brand word (fresh/no-case/
        # disconnect). The dot colour (not the titlebar) signifies connection.
        self._case_title = title
        self.setWindowTitle(title if title else "TRID3NT")

    def _toggle_connection(self) -> None:
        if self.bridge.running:
            self.disconnect_agent()
        else:
            self.connect_agent()

    def _open_settings(self) -> None:
        prev_basemap = self.settings.basemap_preset
        # Item B3 (qgis-ux-batch 2026-07-19): DISCONNECT lives in Settings now
        # (off the header top row). Hand the dialog the dock's disconnect path +
        # the current connection state so its Disconnect button is enabled only
        # while connected and drives the exact same teardown as before.
        dlg = SettingsDialog(
            self.settings,
            self,
            on_disconnect=self.disconnect_agent,
            on_connect=self.connect_agent,
            connected=self.bridge.running,
        )
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        # Item 4: the AOI toggles live only in Settings. Item R3 (2026-07-18):
        # no pinned status line to repaint anymore -- the next send recomputes
        # the AOI and notes any CHANGED notice inline in the transcript.
        # BK-1b: Save persisted the preset but ensure_basemap only ran on
        # case-open/export, so the combo looked dead until the next case
        # switch. An explicit preset change in Settings applies here, not
        # gated on auto_basemap (that checkbox governs automatic adds).
        if self.settings.basemap_preset != prev_basemap:
            note = ensure_basemap(self.settings.basemap_preset)
            if note:
                self._note(note)

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
            self.status_label.setText("Not connected -- open Settings to connect")
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
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self._note("Starting a new case ...")
        # Item C (qgis-ux-batch 2026-07-19): a NEW case must never inherit the
        # PREVIOUS case's AOI rectangle -- drop the overlay + null the local
        # bbox BEFORE creating, so a stale box never lingers into the fresh
        # case (_on_case_open_event repaints from the new case's own bbox, or
        # leaves it cleared when the new case has none). _clear_aoi_overlay
        # also nulls self._case_bbox.
        self._clear_aoi_overlay()
        # A2 (NATE 2026-07-20): a NEW case is BBOX-LESS -- the canvas-as-AOI
        # seed is gone. A clean slate with no AOI until the user Sets one (the
        # Set-AOI rectangle) or the LLM geocodes it.
        self.bridge.case_command("create", args=None)

    def delete_case(self, case_id: str, title: str) -> None:
        """Delete a case (case-command delete). The server re-emits
        case-list, which refreshes the open Cases dialog via ``set_cases``.
        If the deleted case was the dock's active one, clear the case label
        gracefully -- the connection itself stays up."""
        if not self.bridge.running:
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self._note(f"Deleting case '{title}' ...")
        self.bridge.case_command("delete", case_id)
        if case_id == self._case_id:
            self._case_id = None
            # H2 (NATE 2026-07-20): no under-button label anymore -- reset the
            # titlebar to the "TRID3NT" brand word (no active case).
            self._set_case_label("")

    def rename_case(self, case_id: str, new_title: str) -> None:
        """R1 (NATE 2026-07-20): rename a case (case-command ``rename`` with
        ``args={"title": ...}``, the server-supported verb). Mirrors
        ``delete_case``'s bridge send + not-connected guard. The server re-emits
        the case-list, which repaints the open Cases dialog via ``set_cases``;
        if the renamed case is the dock's active one, refresh the titlebar
        label to the new title."""
        new_title = (new_title or "").strip()
        if not new_title:
            return
        if not self.bridge.running:
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self._note(f"Renaming case to '{new_title}' ...")
        self.bridge.case_command("rename", case_id, {"title": new_title})
        if case_id == self._case_id:
            self._set_case_label(new_title)

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
            note = ensure_basemap(self.settings.basemap_preset)
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

    def _register_layer_tree_push_action(self) -> None:
        """Item R6 (live-feedback 2026-07-18): the push action lives in the
        QGIS layer-tree context menu now (right-click a layer -> "Push layer
        to case"), replacing the removed header button. One QAction per layer
        type (vector + raster; the QGIS API registers per-type), both backed
        by ``_push_active_layer`` -- right-clicking a tree entry makes it the
        current/active layer, so the existing active-layer path is exactly
        right. Removed again in ``shutdown`` so a plugin reload never stacks
        duplicate menu entries."""
        for layer_type in (QgsMapLayer.VectorLayer, QgsMapLayer.RasterLayer):
            action = QAction("Push layer to case", self)
            action.triggered.connect(self._push_active_layer)
            try:
                self.iface.addCustomActionForLayerType(
                    action, "", layer_type, True
                )
            except Exception:  # noqa: BLE001 -- headless/stub iface in tests
                continue
            self._push_tree_actions.append(action)

    def _route_compute_step(self, step: PipelineStep) -> None:
        """Item R4 (live-feedback 2026-07-18): a ``role="compute"`` pipeline
        step renders as ONE persistent collapsible SimCard keyed by step_id
        (inserted before the terminal stretch like every message widget) --
        never a transient grey row. Replayed frames fold into the same card;
        ``_clear_messages`` (case switch) resets the registry."""
        card = self._sim_cards.get(step.step_id)
        if card is None:
            # Item N5 (live-feedback 2026-07-19): mirror the BUG-4 gate-card
            # discipline -- close out the current streaming entry BEFORE the
            # card inserts, so post-card narration mints a FRESH entry BELOW
            # it (chronological turn flow; the card never strands at the
            # bottom while text piles above it).
            self._close_pending_for_card()
            card = SimCard(_solver_engine_label(step.tool_name))
            self._sim_cards[step.step_id] = card
            self.messages_layout.insertWidget(
                self.messages_layout.count() - 1, card
            )
            self._scroll_to_bottom()
        card.update_from_step(step)

    def _close_pending_for_card(self) -> None:
        """Items BUG-4 / N5 (live-feedback 2026-07-12 / -07-19): a card (gate
        or sim) is about to insert -- close out the current streaming
        assistant entry so subsequent narration mints a FRESH entry BELOW the
        card via ``_ensure_pending`` (chronological turn flow; the card never
        strands at the bottom while text piles above it). The entry's
        transient pipeline rows are cleared first so they re-render in the new
        entry below rather than duplicating, frozen, above the card."""
        if self._pending is not None:
            self._pending.clear_tool_card()
            # Feature 2026-07-13: this entry receives no more deltas --
            # final-render its markdown before closing it out.
            self._pending.finalize_markdown()
        self._pending = None

    def _note(self, text: str, error: bool = False) -> None:
        self._ensure_pending().add_note(text, error=error)
        self._scroll_to_bottom()

    # -- provider-config (OpenRouter model-extensibility, Feature 3) ----------- #

    def _on_provider_config_finished(self, result: dict) -> None:
        """The agent accepted the live provider config (Settings Save): the
        switch applies on the NEXT message with no restart."""
        model = result.get("model") or "the agent default model"
        host = result.get("base_url_host") or ""
        where = f" via {host}" if host else ""
        self._note(
            f"Provider config applied -- {model}{where} applies on your next "
            "message (no restart)."
        )

    def _on_provider_config_errored(self, message: str) -> None:
        """The agent HTTP listener was unreachable/errored on Save -- the
        settings persisted, but the live push did not land, so keep the honest
        restart-to-apply guidance (the dialog's static note said the same)."""
        self._note(
            "Could not reach the agent to apply the provider config live -- "
            "restart the agent to apply the new provider/key.",
            error=True,
        )

    # -- bridge slots (UI thread) ---------------------------------------------- #

    def _refresh_model_label(self) -> None:
        """Row-1 status TEXT = the active LLM model name (the green dot already
        means "connected"). Prefer the user's Settings model pick; else the
        agent's env default probed on connect; else empty (the dot carries the
        state). NATE 2026-07-20 -- drop the redundant "Connected"/case-id text."""
        model = (self.settings.model_id or self._effective_model or "").strip()
        if model:
            # Short readable form: drop any provider prefix ("nvidia/...") but
            # keep the model id + a ":free" tag; full id lives in the tooltip.
            self.status_label.setText(model.split("/")[-1])
            self.status_label.setToolTip(model)
        else:
            self.status_label.setText("")
            self.status_label.setToolTip("")

    def _probe_effective_model(self) -> None:
        """Ask the agent for its env-default model (off-thread) so the status
        strip shows the real running model even when the Settings picker is
        blank (NATE's nemotron via .env.local). No-op when the user picked a
        model explicitly (that value is authoritative)."""
        if self.settings.model_id:
            return
        task = _EffectiveModelTask(self.settings.export_api, self)
        task.finished.connect(self._on_effective_model)
        self._effective_model_tasks.append(task)  # keep-alive
        task.start()

    def _on_effective_model(self, model_id: str) -> None:
        self._effective_model = model_id or ""
        self._refresh_model_label()

    def _on_connected(self, user_id: str, is_anonymous: bool) -> None:
        self._connected = True
        if is_anonymous and self.settings.mode == MODE_LOCAL:
            self.settings.anonymous_user_id = user_id
        self._refresh_model_label()
        self._probe_effective_model()

    def _on_case_ready(self, case_id: str) -> None:
        self._case_id = case_id
        self.materializer.set_case(case_id, self._session_case_title or None)
        self._set_case_label(self._session_case_title or case_id[:8])
        self._set_dot("connected")
        # NATE 2026-07-20: the status strip carries neither the case-id chip nor
        # a "Connected" word -- the case identity rides the dock TITLEBAR
        # (``_set_case_label``), the green dot means connected, and the status
        # TEXT shows the active model.
        self._refresh_model_label()
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
        self._note(f"Authentication failed: {message}", error=True)

    def _on_closed(self, reason: str) -> None:
        self._connected = False
        self._case_id = None
        if reason == "auth-expired":
            pass  # _on_auth_expired already painted the honest status
        elif reason != "stopped":
            self._set_dot("error")
            self.status_label.setText(f"Disconnected: {reason}")

    def _on_reconnecting(self, reason: str) -> None:
        # Transport lost; the worker's capped-jitter ladder is running.
        self._connected = False
        self._set_dot("connecting")
        self.status_label.setText("Connection lost -- reconnecting ...")

    def _on_resumed(self) -> None:
        self._connected = True
        self._set_dot("connected")
        # Item B1 (qgis-ux-batch 2026-07-19) + NATE 2026-07-20: no case-id chip
        # and no "Reconnected" word in the signifier -- the green dot means
        # connected; the status text returns to the active model name.
        self._refresh_model_label()

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
            # T1..T7 (NATE 2026-07-20): assemble the parent tool card's inner
            # rows + the ONE bottom metadata block. Each inner row is just a
            # tool label + its state (the card renders ">" + label + a status
            # glyph, T4/T5/T6); the per-row arg/state detail that used to ride
            # on the right (T6) is DROPPED from the row and folded into the
            # muted metadata block at the bottom of the card (T7).
            steps = data.get("steps") or []
            inner_rows: List[dict] = []
            meta_lines: List[str] = []
            for step in steps:
                if not isinstance(step, PipelineStep):
                    continue
                if step.tool_name.lower() in _LLM_STEP_NAMES:
                    continue
                if step.role == "compute":
                    # Item R4 (live-feedback 2026-07-18): the off-box solver
                    # step renders as ONE persistent collapsible SimCard, not
                    # an inner tool row -- see _route_compute_step.
                    self._route_compute_step(step)
                    continue
                if step.tool_name == "context:compact":
                    # Compaction narration is not a tool call -- keep it as a
                    # muted metadata line, not a ">" tool row.
                    suffix = (
                        f" ({step.substep_label})" if step.substep_label else ""
                    )
                    meta_lines.append(f"{step.name} - {step.state}{suffix}")
                    continue
                inner_rows.append(
                    {"label": step.tool_name or step.name,
                     "state": step.state,
                     "nested": bool(step.parent_step_id)}
                )
                # T7: the arg summary (was the per-row right-side detail, T6)
                # + any substep label + error text join the bottom metadata.
                bits: List[str] = []
                if step.substep_label:
                    bits.append(step.substep_label)
                args = self._tool_args_by_step.get(step.step_id)
                if args:
                    bits.append(args)
                if bits:
                    meta_lines.append(
                        f"{step.tool_name or step.name}: " + "  ".join(bits)
                    )
                if step.error_message:
                    meta_lines.append(
                        f"{step.tool_name or step.name}: {step.error_message}"
                    )
            self._ensure_pending().render_tool_card(inner_rows, meta_lines)
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
        elif kind == "chart":
            # OpenQuake result parity (live-feedback 2026-07-13): a live
            # mid-turn chart. The Charts panel renders it (never a chat
            # widget); one pointer note in the transcript so the turn's
            # narrative says where the chart went.
            if self.charts_panel.add_chart(data):
                title = data.get("title") or "chart"
                self._ensure_pending().add_note(
                    f"Chart added below: {title}"
                )
                self._scroll_to_bottom()
        elif kind == "solve-progress":
            # Item R4 (live-feedback 2026-07-18): the ~10 s big-sim telemetry
            # tick. It carries run_id, not step_id; the local seam runs one
            # sim at a time, so fold it into every non-terminal SimCard (the
            # card itself run_id-stamps and ignores live-only fields once
            # terminal -- a straggler tick never repaints a finished card).
            for card in self._sim_cards.values():
                if not card.terminal:
                    card.update_from_progress(data)
        elif kind == "tool-io":
            # Item R2 (live-feedback 2026-07-18): raw-args sidecar keyed by
            # step_id (emitted at dispatch START, so the summary is in place
            # before the pipeline frame paints the chip row).
            sid = data.get("step_id")
            raw = data.get("raw_args")
            if isinstance(sid, str) and sid and isinstance(raw, str):
                self._tool_args_by_step[sid] = _short_args_summary(raw)
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
        elif kind == "raw" and data.get("type") == "map-command":
            # Item G (qgis-ux-batch 2026-07-19): the agent frames a mesh preview
            # (and other explicit re-frames) via a "zoom-to" map-command, since
            # the preview layer itself is published role=input with bbox=None
            # (so it does NOT self-zoom -- that would yank the camera for every
            # silent input layer). The plugin dropped every map-command (no
            # etype branch in trid3nt_client._classify -> arrives here as
            # kind="raw"), so the fine EPSG:4326 wireframe sat sub-pixel under
            # the AOI, invisible. Honor ONLY the explicit zoom-to here: frame
            # the mesh (which sends one) without disturbing silent input layers.
            payload = data.get("payload") or {}
            if payload.get("command") == "zoom-to":
                bbox = (payload.get("args") or {}).get("bbox")
                try:
                    canvas = self.iface.mapCanvas()
                except Exception:  # noqa: BLE001 -- headless: nothing to zoom
                    canvas = None
                if (
                    canvas is not None
                    and isinstance(bbox, (list, tuple))
                    and len(bbox) == 4
                ):
                    zoom_to_bbox4326(canvas, tuple(bbox))
        elif kind == "turn-complete":
            if self._pending is not None:
                # Feature 2026-07-13: the answer text is final -- convert
                # the plain streamed text to rendered markdown now.
                self._pending.finalize_markdown()
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
        self._refresh_model_label()  # status text = active model, not case-id
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
        # OpenQuake result parity (live-feedback 2026-07-13): replay the
        # case's persisted charts into the Charts panel (the web shows the
        # hazard-curve card on case open; the dock now matches). The panel
        # stays hidden for chart-less cases -- no chat noise either way.
        self.charts_panel.set_charts(info.charts)
        if self.settings.auto_basemap:
            note = ensure_basemap(self.settings.basemap_preset)
            if note:
                self._note(note)
        self._zoom_after_case_open(info)
        # Per-case-bbox 2026-07-19: show the just-opened case's persisted AOI
        # as the dashed overlay (the exact bbox the agent references each turn
        # via state.case_bbox), so the user sees + can re-draw it. A bbox-less
        # case leaves the overlay cleared (already reset in _clear_messages).
        self._case_bbox = info.bbox
        if info.bbox is not None:
            self._render_aoi_overlay(info.bbox)
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
        card = GateCard(
            warning, self._on_gate_decision,
            iface=self.iface, to_lonlat=self._point_to_lonlat4326,
        )
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        # BUG 4 (live-feedback 2026-07-12, NATE: the card sat "at the bottom
        # and the response chat is above") / Item N5 (2026-07-19): the
        # streaming _AssistantEntry was created BEFORE the card, so everything
        # after the user's confirm streamed into the entry ABOVE it. Close out
        # the pending entry here (shared _close_pending_for_card discipline) --
        # the next event (thinking/chunk/pipeline/note) mints a FRESH entry
        # via _ensure_pending, which inserts before the terminal stretch, i.e.
        # BELOW the card -- the cloud web's chronological turn flow.
        self._close_pending_for_card()
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
        # Item A (qgis-ux-batch 2026-07-19): multi-line composer -- read the
        # full document (toPlainText), not the one-line QLineEdit .text().
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        if not (self._case_id and self.bridge.running):
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self.input_edit.clear()
        self._add_user_bubble(text)
        if self._pending is not None:
            # Feature 2026-07-13, defensive: a WS drop can strand a
            # streaming entry that never saw turn-complete -- final-render
            # it before minting the new turn's entry.
            self._pending.finalize_markdown()
        self._pending = _AssistantEntry(self.messages_layout)
        self._scroll_to_bottom()
        bbox, source = self._aoi_for_send()
        # O1 (NATE 2026-07-20): no per-send AOI note anymore -- the bbox still
        # rides the wire text (below), but the transcript note fires only WHEN
        # the user sets/changes the AOI (``_on_aoi_extent_chosen``), never as a
        # standing restated readout.
        wire_text = (
            aoi.attach_aoi_to_text(text, bbox, source=source or "canvas")
            if bbox
            else text
        )
        try:
            # F9: pass show_thinking so the server enables reasoning-channel
            # forwarding for this turn (local mode only; remote ignores the field).
            # OpenRouter model-extensibility (design 2026-07-19): ride the picked
            # model_id (empty = agent env default) so a MODEL switch applies
            # live on the next message with no agent restart -- mirrors the
            # show_thinking add. Provider base_url/key stay agent-process env.
            self.bridge.send_chat(
                wire_text,
                show_thinking=self.settings.show_thinking,
                model_id=self.settings.model_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"send failed: {exc}", error=True)

    # -- teardown ------------------------------------------------------------- #

    def shutdown(self) -> None:
        # Item R6 (live-feedback 2026-07-18): unhook the layer-tree push
        # actions so a plugin reload never stacks duplicate menu entries.
        for action in self._push_tree_actions:
            try:
                self.iface.removeCustomActionForLayerType(action)
            except Exception:  # noqa: BLE001
                pass
        self._push_tree_actions = []
        self.bridge.stop()
