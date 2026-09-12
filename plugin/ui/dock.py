"""TRID3NT chat dock -- message list, input, status dot, settings.

ALL socket work lives on the bridge worker thread; this widget only handles Qt
signals. Layers STREAM in place through the store endpoint: there is zero
user-facing export, because native QGIS already covers file export."""

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

# The layer-type constant the "Push layer to case" context-menu action is
# registered against.
from qgis.core import QgsMapLayer

from . import gate
from .charts_window import ChartsWindow
from .cards import (
    CodeExecCard,
    CredentialCard,
    FormCard,
    GateCard,
    RegionChoiceCard,
    SimCard,
    SpatialInputCard,
    ToolCandidatesCard,
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
from ..case import aoi, push_layer
from ..net.tasks import (
    _CaseListTask,
    _EffectiveModelTask,
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
    resolve_data_base,
    resolve_http_base,
)
from ..net.run_invocation import USAGE as _RUN_USAGE_HINT, parse_run_invocation
from ..net.ws_bridge import AgentBridge
from ..plugin_settings import PluginSettings
from ..render import probe
from ..render.processing import run_processing_request
from ..render.layers import (
    LayerMaterializer,
    configure_store_access,
    ensure_basemap,
    sweep_stale_session_dirs,
    zoom_to_bbox4326,
    zoom_to_extent,
)



# LLM bookkeeping step names the dock hides from the tool timeline. The server
# emits the provider-neutral ``model_generate`` for the model-stream step; the
# rest are neutral synonyms tolerated across step-naming variation.
_LLM_STEP_NAMES = {
    "llm_generation", "thinking", "llm",
    "model_generate", "generate",
}

# Picker fail-open: event kinds that mean the
# TURN MOVED ON -- any of these arriving while a ToolCandidatesCard is still
# unanswered proves the server's ``timeout_s`` fail-open (or a cancel/error)
# already resolved that selection, so the dock folds the open card to its
# "agent proceeded" chip. Housekeeping kinds (session-state / case-list /
# solve-progress) are deliberately absent -- they can arrive while the server
# is still genuinely waiting on the pick.
_PICKER_SUPERSEDE_KINDS = {
    "thinking-chunk", "chunk", "pipeline", "tool-io", "turn-complete", "error",
}

_DOT_STYLE = "border-radius: 6px; min-width: 12px; max-width: 12px; min-height: 12px; max-height: 12px;"
_DOT_COLORS = {
    "disconnected": "#8b949e",
    "connecting": "#d29922",
    "connected": "#3fb950",
    "error": "#f85149",
}
# The coloured DOT alone signifies connection state; the titlebar carries the
# active CASE NAME instead.

_USER_BUBBLE_STYLE = (
    "background-color: #1f6feb; color: white; border-radius: 8px; padding: 6px 9px;"
)


def _run_identity(engine: str, module: str) -> str:
    """A run's identity for a sim card title: the ENGINE and the MODULE of it
    that ran. A run that states no engine is titled by neither - the full run
    identity stays in the card's metadata table either way."""
    if not engine:
        return "SOLVER"
    return f"{engine.upper()} / {module}" if module else engine.upper()


def _short_args_summary(raw_args: str, max_len: int = 64) -> str:
    """A compact ``k=v`` arg summary for a tool row: the first three keys,
    each value clipped, nested structures collapsed. This is a SUMMARY, not an
    IO dump; unusable input yields "" and the row renders without args."""
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
        # Remote-streaming session TTL: sweep crash-leftover staging
        # dirs from prior sessions before this session opens its own. Only DEAD
        # owners are swept -- a concurrent live QGIS instance keeps its dir.
        sweep_stale_session_dirs()
        self.materializer = LayerMaterializer(self.settings)
        self._pending: Optional[_AssistantEntry] = None
        self._connected = False
        # Remote-daemon (tailnet) endpoint derivation: server-advertised
        # ``http_base`` / ``data_base`` from the last connect handshake (None
        # until a daemon that advertises them acks). See
        # ``_effective_http_base`` / ``_effective_data_base`` -- every
        # :8766 caller and the store's GDAL configuration resolve through
        # those two so a fresh daemon's advertisement always wins and an old
        # daemon still falls back honestly.
        self._advertised_http_base: Optional[str] = None
        self._advertised_data_base: Optional[str] = None
        self._case_id: Optional[str] = None
        self._case_title: str = ""
        self._session_case_title: str = ""
        self._cases: List[CaseInfo] = []
        self._cases_dialog: Optional[CasesDialog] = None
        self._case_list_tasks: List[_CaseListTask] = []  # keep-alive refs
        self._push_tasks: List[_PushLayerTask] = []  # keep-alive refs
        self._probe_tasks: List[_ProbePointTask] = []  # keep-alive refs
        # The Settings Save
        # provider-config POST tasks -- owned here (not the closing dialog).
        self._provider_config_tasks: List[_ProviderConfigTask] = []
        # The Probe map tool (design point 2): built lazily on first toggle-on
        # (QgsMapToolEmitPoint needs a live canvas). ``_prev_map_tool`` is the
        # canvas' tool saved right before the Probe tool is installed, so
        # toggling off restores it (never steals the tool permanently).
        self._probe_map_tool = None
        self._prev_map_tool = None
        # The persistent per-case bbox:
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
        # the 'Draw region' supply path -- ONE rubber-band rectangle
        # attached to the NEXT chat turn as ``drawn_geometry`` (a basis="user"
        # spatial knob the composer gates consume, e.g. geoclaw amr_regions).
        # ``_drawn_region`` is the pending ``{"geometry_type": "rectangle",
        # "bbox": [w,s,e,n]}`` payload (None until drawn), CLEARED on send (one
        # rectangle, one turn). Its map tool + overlay mirror the Set-AOI pair.
        self._drawn_region: Optional[dict] = None
        self._region_rubber = None
        self._region_map_tool = None
        self._prev_region_tool = None
        # True while the Set-AOI canvas key-filter (BACKSPACE
        # /DELETE -> _clear_aoi) is installed -- see ``_toggle_aoi_draw``.
        self._aoi_key_filter_on = False
        self._refresh_debounce = Debouncer()
        # A case picked from the Cases
        # dialog before/while connecting -- opened via ``_on_case_ready``
        # once the (auto-)connect actually completes, so a cold-list click
        # is never silently dropped.
        self._pending_open_case: Optional[Tuple[str, str]] = None
        # AUTO-CONNECT fires once per dock SHOW,
        # reset on hide -- see ``showEvent``/``hideEvent``/``_auto_connect_local_once``.
        self._auto_connect_done_this_show = False
        # Live SimCards keyed by the
        # compute step's step_id; reset on case switch (_clear_messages).
        self._sim_cards: Dict[str, SimCard] = {}
        # Short arg summaries from the
        # tool-io sidecar, keyed by step_id, for the tool chip rows.
        self._tool_args_by_step: Dict[str, str] = {}
        # The OPEN (not yet answered/
        # superseded) tool-picker cards, in arrival order. A subsequent turn
        # event folds them to "agent proceeded" (_supersede_open_tool_pickers);
        # reset on case switch (_clear_messages -- the widgets die with the
        # message list).
        self._open_tool_pickers: List[ToolCandidatesCard] = []
        # Approved code-exec cards keyed by code_exec_id so the
        # processing-request that follows an approval can fold its outcome
        # into the right card's chip; reset on case switch
        # (_clear_messages -- the widgets die with the message list).
        self._code_exec_cards: Dict[str, CodeExecCard] = {}
        # The latest ``secrets-list`` roster (parsed
        # SecretRow list) for the settings/secrets surface. Minimal honest
        # handling -- stored + a one-line status note; raw keys never ride
        # here. Reset on case switch.
        self._secrets: list = []
        # 1-based counter of picker
        # cards shown THIS turn -- the "Step N" affordance is trivially
        # derived from card arrival order, never a server field. Reset on
        # every new user turn (_send) and on a case switch (_clear_messages).
        self._tool_picker_turn_step = 0
        # The standing "AOI: drawn X x Y deg" readout is
        # GONE (clutter). The ONLY AOI note is the one-time inline transcript
        # note ``_on_aoi_extent_chosen`` emits WHEN the user actually sets the
        # AOI ("Case AOI set to ..."). Nothing is restated per-send anymore, so
        # the old ``_aoi_status_line`` / ``_last_aoi_note`` dedupe pair is
        # removed with it.

        # The charts window: a bottom-docked
        # directive -- charts get their OWN bottom-docked window, never an
        # in-chat panel. Built LAZILY (first chart or first "Charts (N)" button
        # click) so a chart-less session never spawns a bottom dock. The chat
        # keeps only the count button; ``_charts_count`` drives its label.
        self._charts_window: Optional[ChartsWindow] = None
        self._charts_count = 0

        self._build_ui()
        self._wire_bridge()
        self._configure_store_access()
        # The persistent "Push layer"
        # header row stays clean -- the push action lives
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
        """Connect without the user pressing Connect, ONCE per dock show and
        reset on hide. It never retries within one show: a failure paints the
        same honest status line a manual click would."""
        if self._auto_connect_done_this_show:
            return
        self._auto_connect_done_this_show = True
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
        # a pure status strip.
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

        # Row 2, the action buttons. Connect and disconnect BOTH live in
        # Settings, not here: the dock auto-connects, so a greyed header button
        # would be noise.
        button_row = QHBoxLayout()
        self.cases_btn = QToolButton()
        self.cases_btn.setText("Cases")
        self.cases_btn.clicked.connect(self._open_cases)
        button_row.addWidget(self.cases_btn)

        # Case creation lives in the Cases dialog, and pushing a layer lives
        # in the QGIS layer-tree context menu; neither has a header button.

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

        # The persistent per-case bbox: drag a rectangle
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

        # 'Draw region' -- drag ONE rectangle that rides the NEXT chat
        # turn as ``drawn_geometry`` (a basis="user" spatial knob for composer
        # gates, e.g. geoclaw amr_regions). Distinct from Set AOI (the analysis
        # extent): a drawn region is a sub-region knob, cleared on send.
        self.region_btn = QToolButton()
        self.region_btn.setText("Draw region")
        self.region_btn.setCheckable(True)
        self.region_btn.setToolTip(
            "Drag a rectangle to attach to your next message as a refinement "
            "region (e.g. where GeoClaw refines its mesh). Cleared on send."
        )
        self.region_btn.toggled.connect(self._toggle_draw_region)
        button_row.addWidget(self.region_btn)

        # The count button that SHOWS the
        # bottom charts window. It stays in the chat dock; charts themselves
        # never render inline here. Click raises (creates lazily) the window;
        # a new chart increments the count + subtly flags the button.
        self.charts_btn = QToolButton()
        self.charts_btn.setText("Charts (0)")
        self.charts_btn.setToolTip("Show the charts window (bottom of the app)")
        self.charts_btn.clicked.connect(self._show_charts_window)
        button_row.addWidget(self.charts_btn)

        # Clearing the AOI is MULTIPLEXED into the Set-AOI tool: while it is
        # active, BACKSPACE or DELETE clears the current AOI through a canvas
        # eventFilter installed only for the duration.

        # Settings is a COG GLYPH, so no word label competes with the
        # connection signifier; the tooltip names it.
        button_row.addStretch(1)  # push Settings to the right end of the row
        self.settings_btn = QToolButton()
        self.settings_btn.setText("\u2699")  # gear glyph (cog); icon-only look
        self.settings_btn.setToolTip("Settings (connect / disconnect live here)")
        self.settings_btn.clicked.connect(self._open_settings)
        button_row.addWidget(self.settings_btn)
        outer.addLayout(button_row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)

        # Message list
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # The transcript NEVER grows a horizontal
        # scrollbar -- error text (and every other chat line) must reflow with
        # the resizable chat panel, not pin it wide. AlwaysOff (was the default
        # ScrollBarAsNeeded, which a long unbroken error token could trip);
        # wrapped labels cap their minimum width to 1 so content reflows.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.messages_host = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_host)
        self.messages_layout.setContentsMargins(2, 2, 2, 2)
        self.messages_layout.setSpacing(4)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_host)
        outer.addWidget(self.scroll, 1)

        # (the probe should
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
        self.probe_result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.probe_result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.probe_result_label.setStyleSheet(_THINKING_BLOCK_STYLE)
        probe_lay.addWidget(self.probe_result_label)
        self._probe_panel.setVisible(False)
        outer.addWidget(self._probe_panel)

        # The charts surface is the bottom-docked window, NEVER an in-chat
        # panel; this dock keeps only the count button in the row above.
        #
        # The ONLY AOI note is the one-time line emitted when the user actually
        # sets one: there is no pinned status line and no per-send restatement.
        #
        # The input row: ENTER sends and SHIFT+ENTER newlines, so the composer
        # is the sole full-width widget in it.
        input_row = QHBoxLayout()
        self.input_edit = _ChatInput(self._send)
        self.input_edit.setPlaceholderText("Ask for data or a simulation...")
        input_row.addWidget(self.input_edit, 1)
        outer.addLayout(input_row)

        self.setWidget(body)

    def _set_dot(self, state: str) -> None:
        color = _DOT_COLORS.get(state, _DOT_COLORS["disconnected"])
        self.dot.setStyleSheet(f"background-color: {color}; {_DOT_STYLE}")
        # Nothing to repaint here: the titlebar carries the CASE NAME, not
        # the connection state.

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add_user_bubble(self, text: str) -> None:
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(40, 2, 0, 2)
        lbl = _WrapLabel(text)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet(_USER_BUBBLE_STYLE)
        # The bare QSizePolicy(h, v) ctor
        # DROPS the height-for-width flag QLabel.setWordWrap had set, which
        # (with the AlignRight cell) clipped long messages to one visual
        # line. Restore the flag so the layout asks the label how tall its
        # wrapped text is; _WrapLabel's min-height re-assert covers the
        # layout paths that still ignore height-for-width. Horizontal
        # Maximum + AlignRight keep the hug-the-text right-aligned look.
        policy = QSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        lbl.setSizePolicy(policy)
        lay.addWidget(lbl, 0, Qt.AlignmentFlag.AlignRight)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, container)
        self._scroll_to_bottom()

    def _ensure_pending(self) -> _AssistantEntry:
        if self._pending is None:
            self._pending = _AssistantEntry(self.messages_layout)
        return self._pending

    def _clear_messages(self) -> None:
        """Remove every message-list child widget while KEEPING the terminal
        stretch item, which every other insertion goes before. Drops the
        pending streaming entry: a stale target must never take new deltas."""
        self._pending = None
        # Per-case transcript state: the sim-card registry, whose widgets die
        # in the loop below, and the tool arg summaries.
        self._sim_cards.clear()
        self._tool_args_by_step.clear()
        # open picker cards are per-case transcript state -- the
        # widgets die in the loop below; drop the tracking refs with them.
        self._open_tool_pickers = []
        self._tool_picker_turn_step = 0
        # Approved code-exec cards are per-case transcript
        # state -- the widgets die in the loop below; drop the tracking refs.
        # (self._secrets is user/Case-level roster state, refreshed by the next
        # secrets-list; a switch clears it so a stale roster never lingers.)
        self._code_exec_cards.clear()
        self._secrets = []
        # The previous case's AOI overlay must not
        # linger across a switch -- _on_case_open_event repaints it below from
        # the newly-opened case's own bbox (or leaves it cleared when absent).
        self._clear_aoi_overlay()
        # a pending drawn region is per-turn/per-case -- drop it on a
        # case switch so it never rides a turn in the wrong case.
        self._clear_region_overlay()
        # The probe panel shows CASE
        # data -- a table from the previous case must not linger across a
        # switch. Hide it (its next click repopulates it).
        self._probe_panel.setVisible(False)
        self.probe_result_label.setText("")
        # Charts are per-Case state: the case-open
        # replay below repopulates the charts window for the new case. The
        # window survives the switch (bottom dock stays put); only its list is
        # cleared + the count button reset.
        if self._charts_window is not None:
            self._charts_window.clear()
        self._charts_count = 0
        self._set_charts_button()
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _replay_chat_history(self, messages: List[dict]) -> None:
        """Repaint a just-opened case's persisted conversation. A run of
        consecutive tool rows groups into ONE parent card, the same widget the
        live pipeline builds, so a reopened case reads identically."""
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
            # wrapped inline error line, same place it appeared live;
            # CONSECUTIVE persisted error rows fold into one collapsed
            # "ERRORS (N)" row exactly like live arrival (they funnel through
            # the same add_note path on the same pending entry).
            if role == "error" or row.get("is_error"):
                if isinstance(content, str) and content:
                    self._note(content, error=True)
                continue
            if not isinstance(content, str) or not content:
                continue
            # A conversational row ends any open consecutive-error run --
            # persisted errors separated by chat must not glue into one fold.
            if self._pending is not None:
                self._pending.break_error_run()
            if role == "user":
                self._add_user_bubble(content)
            elif role == "agent":
                entry = _AssistantEntry(self.messages_layout)
                # Replay persisted reasoning as
                # the SAME grey collapsible thinking fold the live
                # agent-thinking-chunk path builds -- collapsed by default,
                # above the answer text in the same bubble.
                # parse_chat_history now surfaces "thinking" on agent rows
                # (None when absent); "reasoning" stays as a defensive alias.
                thinking = row.get("thinking") or row.get("reasoning")
                if isinstance(thinking, str) and thinking.strip():
                    entry.append_thinking_delta(thinking)
                    entry.collapse_thinking()
                entry.append_delta(content)
                # Replayed text is always final --
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
        """Render a run of persisted tool rows as ONE parent card, each row
        carrying its response as a collapsed read-only body. The card is
        all-terminal on replay, so it auto-collapses like a finished live one."""
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
        """A QgsRectangle in ``authid`` -> an EPSG:4326 bbox tuple, or None.
        Never raises: an unresolvable CRS yields None and the status line says
        so honestly."""
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
        """The active layer's SELECTION bbox as EPSG:4326, or None. The bbox
        OF the selection, never its ring: every AOI carrier on the wire is a
        four-number box. A degenerate rect has no area and yields None."""
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
        The AOI model is EXPLICIT-only: either the user DREW one, or none is
        set and the agent geocodes the location out of the message itself."""
        bbox = self._case_bbox
        if bbox is not None and aoi.bbox_within_guard(bbox):
            return bbox, "drawn"
        return None, None

    # -- probe (map-click point sample) --------------------------------------- #

    def _point_to_lonlat4326(
        self, point, authid: str
    ) -> Optional[Tuple[float, float]]:
        """A clicked ``QgsPointXY`` in ``authid`` -> EPSG:4326 ``(lon, lat)``,
        or None. Never raises: an unresolvable CRS or an out-of-range result
        yields None and the click note says so."""
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
        """Install or restore the canvas map tool for the Probe button. ON
        SAVES whatever tool was active first, and OFF restores it, so the
        canvas is never left on a tool the user did not ask for."""
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
        """The latest probe status, result or error goes to the PINNED panel
        under the message list, replaced in place on each click -- never a
        chat note."""
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
        base_url = self._effective_http_base()
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

    # -- case AOI (the persistent per-case bbox) ------------------------------- #

    def _render_aoi_overlay(
        self, bbox4326: Tuple[float, float, float, float]
    ) -> None:
        """Paint the case AOI as a DASHED, outline-only rectangle, so it reads
        as an EXTENT and not a filled feature. Headless-safe: no canvas is a
        silent no-op, and ``_case_bbox`` remains authoritative regardless."""
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
                    self._aoi_rubber.setLineStyle(Qt.PenStyle.DotLine)
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
        """Install or restore the canvas map tool for the Set-AOI button, ON
        saving the active tool and OFF restoring it. GUARDED: without a live
        case and connection the button snaps back off with an honest note."""
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
            # While Set-AOI is ON, BACKSPACE/DELETE clears
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
        """While Set-AOI is active, BACKSPACE or DELETE on the canvas clears
        the AOI. Guarded on both flag and key, and every other event falls
        through, so nothing else on the canvas is intercepted."""
        from qgis.PyQt.QtCore import QEvent

        if (
            getattr(self, "_aoi_key_filter_on", False)
            and self.aoi_btn.isChecked()
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
        ):
            self._clear_aoi()
            return True
        return super().eventFilter(obj, event)

    def _on_aoi_extent_chosen(self, rect) -> None:
        """A rectangle was dragged with Set-AOI: convert it to EPSG:4326,
        repaint the overlay, PERSIST it on the case so every later turn anchors
        on it, then restore the prior map tool."""
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
        """Clear the current case AOI locally AND server-side, so the agent
        stops anchoring on the old extent; an empty bbox IS the reset. Without
        a live case there is nothing to sync, and it says so."""
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

    # -- draw-a-region supply path

    def _toggle_draw_region(self, checked: bool) -> None:
        """Install or restore the Draw-region map tool, ON saving the active
        tool and OFF restoring it. Unlike Set-AOI this is allowed OFFLINE,
        because the region attaches to the next message rather than the case."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no canvas -- no-op
            return
        if checked:
            if self._region_map_tool is None:
                try:
                    from qgis.gui import QgsMapToolExtent

                    self._region_map_tool = QgsMapToolExtent(canvas)
                    self._region_map_tool.extentChanged.connect(
                        self._on_region_extent_chosen
                    )
                except Exception:  # noqa: BLE001 -- older build lacks the tool
                    self._note(
                        "Draw region is unavailable in this QGIS build "
                        "(QgsMapToolExtent missing).",
                        error=True,
                    )
                    self.region_btn.blockSignals(True)
                    self.region_btn.setChecked(False)
                    self.region_btn.blockSignals(False)
                    return
            self._prev_region_tool = canvas.mapTool()
            canvas.setMapTool(self._region_map_tool)
        else:
            if canvas.mapTool() is self._region_map_tool:
                canvas.setMapTool(self._prev_region_tool)
            self._prev_region_tool = None

    def _on_region_extent_chosen(self, rect) -> None:
        """A rectangle was dragged with Draw-region: stash it as the pending
        drawn geometry and paint the overlay. It rides the NEXT message and is
        cleared on send -- one rectangle, one turn."""
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
                "Could not set the region: the drawn extent did not resolve "
                "to EPSG:4326.",
                error=True,
            )
            self.region_btn.setChecked(False)  # restores the prior tool
            return
        self._drawn_region = {
            "geometry_type": "rectangle",
            "bbox": list(bbox),
        }
        self._render_region_overlay(bbox)
        self._note(
            f"Region drawn {aoi.format_bbox(bbox)} -- attaches to your next "
            "message, then clears."
        )
        self.region_btn.setChecked(False)

    def _render_region_overlay(
        self, bbox4326: Tuple[float, float, float, float]
    ) -> None:
        """Paint the pending drawn region as a SOLID amber outline, distinct
        from the dashed AOI overlay, so the user sees what will attach.
        Headless is a silent no-op; the stored region drives the send."""
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
            if self._region_rubber is None:
                self._region_rubber = QgsRubberBand(
                    canvas, QgsWkbTypes.PolygonGeometry
                )
                self._region_rubber.setColor(QColor("#e3b341"))  # amber
                self._region_rubber.setWidth(2)
                self._region_rubber.setFillColor(QColor(0, 0, 0, 0))
            self._region_rubber.setToGeometry(QgsGeometry.fromRect(rect), None)
            self._region_rubber.show()
        except Exception:  # noqa: BLE001 -- overlay is cosmetic; never crash
            return

    def _clear_region_overlay(self) -> None:
        """Drop the pending drawn region + hide its overlay. Called on send
        (clear-on-send) and on case switch / disconnect so a stale region never
        rides a later turn or lingers on the canvas."""
        self._drawn_region = None
        if self._region_rubber is None:
            return
        try:
            from qgis.core import QgsWkbTypes

            self._region_rubber.reset(QgsWkbTypes.PolygonGeometry)
            self._region_rubber.hide()
        except Exception:  # noqa: BLE001 -- best-effort teardown
            pass

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

    def _effective_http_base(self) -> str:
        """The resolved agent HTTP base, and the ONE derivation seam every
        caller of it goes through, so they cannot drift apart. It prefers the
        advertised base and otherwise derives one from the Server URL host."""
        return resolve_http_base(self._advertised_http_base, self.settings.local_url)

    def _effective_data_base(self) -> str:
        """The object store's endpoint. Prefers the server-advertised
        ``data_base``; falls back to ``settings.minio_endpoint`` for daemons
        that do not advertise it."""
        return resolve_data_base(self._advertised_data_base, self.settings.minio_endpoint)

    def _configure_store_access(self) -> None:
        """Point GDAL's ``/vsis3`` at the effective store endpoint.
        IDEMPOTENT, and run both at construction and after each connect: the
        endpoint is only certain once advertised, but layers can paint first."""
        note = configure_store_access(
            self._effective_data_base(),
            self.settings.store_access_key,
            self.settings.store_secret_key,
            self.settings.store_region,
        )
        if note:
            self._note(note, error=True)

    def connect_agent(self) -> None:
        if self.bridge.running:
            return
        url = self.settings.effective_url()
        if not url:
            self.status_label.setText("Set the agent URL in Settings first")
            self._set_dot("error")
            return
        self._set_dot("connecting")
        self.status_label.setText(f"Connecting to {url} ...")
        # The top-row button only CONNECTS;
        # disable it while a connection is up/in-flight (Disconnect lives in
        # Settings now). Re-enabled by disconnect_agent + the failure paths.
        title = "QGIS session " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._session_case_title = title
        # A fresh case is BBOX-LESS -- never seed the
        # canvas extent on create. The AOI is set explicitly later (the Set-AOI
        # rectangle) or the agent geocodes it; there is no canvas-as-AOI path.
        self.bridge.start(
            url,
            token=self.settings.effective_token(),
            case_title=title,
            case_bbox=None,
            # REUSE the resumed / newest existing
            # case instead of minting a fresh "QGIS session ..." case on every
            # connect (with auto-connect that regrew case clutter per
            # dock-show); create only when zero cases exist.
            reuse_case=True,
        )

    def disconnect_agent(self) -> None:
        self.bridge.stop()
        self._connected = False
        self._case_id = None
        # Remote-streaming session TTL: a disconnect ends the
        # session, so sweep everything staged this session (the ONE fallback
        # for non-streamable meshes) -- nothing outlives the session.
        self.materializer.cleanup_session()
        # The case AOI is per-case state -- a
        # disconnect ends the case binding, so the overlay must go too.
        self._clear_aoi_overlay()
        self._clear_region_overlay  #: drop any pending drawn region
        self._set_case_label("")
        self._set_dot("disconnected")
        self.status_label.setText("Not connected")
        # Re-arm the top-row Connect button (disabled while connected).

    def _set_case_label(self, title: str) -> None:
        # ONE method both the case-open and the no-case paths call: a title
        # names that case in the titlebar, and empty is the brand word. The
        # dot colour, never the titlebar, signifies connection.
        self._case_title = title
        self.setWindowTitle(title if title else "TRID3NT")

    def _toggle_connection(self) -> None:
        if self.bridge.running:
            self.disconnect_agent()
        else:
            self.connect_agent()

    def _open_settings(self) -> None:
        prev_basemap = self.settings.basemap_preset
        # DISCONNECT lives in Settings now
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
        dlg.exec()
        # The AOI toggles live only in Settings, and
        # no pinned status line to repaint anymore -- the next send recomputes
        # the AOI and notes any CHANGED notice inline in the transcript.
        # Save persisted the preset but ensure_basemap only ran on
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
        # Populate from the cold HTTP
        # route when there is nothing to show yet, or the live WS case-list
        # never arrived (not connected) -- so the dialog is never an honest-
        # looking-but-wrong empty state while the agent box actually has
        # cases sitting in Persistence.
        if not self._cases or not self._connected:
            self._load_cold_case_list(dlg)
        try:
            dlg.exec()
        finally:
            self._cases_dialog = None

    def _load_cold_case_list(self, dlg: "CasesDialog") -> None:
        """Fetch the COLD case list off the UI thread and feed it into
        ``dlg``, so the dialog can populate before any connection exists. A
        failure lands as an honest note in the dialog."""
        dlg.info_lbl.setText("Loading cases ...")
        base_url = self._effective_http_base()
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

    # -- case switching / new / delete ----------------------------------------- #

    def open_case(self, case_id: str, title: str) -> None:
        """Open ``case_id`` from the Cases dialog, cold-listed or not. When
        not yet connected the open is QUEUED and performed once the handshake
        completes, so a click is never silently dropped."""
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
        """Switch the chat session to an existing case. This only stamps
        optimistically and sends: the server's ``case-open`` reply is what
        actually rebinds the dock."""
        if not self.bridge.running:
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self._case_id = case_id
        self._note(f"Switching to case '{title}' ...")
        self.bridge.select_case(case_id)

    def new_case(self) -> None:
        """Start a fresh case. The server's ``case-open`` reply rebinds the
        dock through the SAME path a case select rebinds through."""
        if not self.bridge.running:
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self._note("Starting a new case ...")
        # A NEW case must never inherit the
        # PREVIOUS case's AOI rectangle -- drop the overlay + null the local
        # bbox BEFORE creating, so a stale box never lingers into the fresh
        # case (_on_case_open_event repaints from the new case's own bbox, or
        # leaves it cleared when the new case has none). _clear_aoi_overlay
        # also nulls self._case_bbox.
        self._clear_aoi_overlay()
        self._clear_region_overlay  #: drop any pending drawn region
        # A NEW case is BBOX-LESS: a clean slate with no AOI until the user
        # sets one, or the model geocodes it out of the message.
        self.bridge.case_command("create", args=None)

    def delete_case(self, case_id: str, title: str) -> None:
        """Delete a case. The server re-emits the case list, which refreshes
        an open dialog; deleting the ACTIVE case clears the label but leaves
        the connection up."""
        if not self.bridge.running:
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self._note(f"Deleting case '{title}' ...")
        self.bridge.case_command("delete", case_id)
        if case_id == self._case_id:
            self._case_id = None
            # No under-button label anymore -- reset the
            # titlebar to the "TRID3NT" brand word (no active case).
            self._set_case_label("")

    def rename_case(self, case_id: str, new_title: str) -> None:
        """Rename a case. The server re-emits the case list, which repaints an
        open dialog; renaming the ACTIVE case refreshes the titlebar label."""
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

    # -- push active layer into the case -------------------------------------- #

    def _push_active_layer(self) -> None:
        """Send the ACTIVE QGIS layer into the current case as a first-class
        input layer. Exactly ONE confirm, for the Set-as-case-AOI checkbox,
        because that alone mutates the case extent."""
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
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
        aoi_checkbox = QCheckBox("Set as case AOI")
        aoi_checkbox.setChecked(False)
        box.setCheckBox(aoi_checkbox)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        make_aoi = aoi_checkbox.isChecked()

        base_url = self._effective_http_base()
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
        """Register the push action on the QGIS layer-tree context menu, one
        QAction per layer type. Right-clicking a tree entry makes it ACTIVE,
        so the active-layer path is exactly the right one to back it with."""
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
        """A compute pipeline step renders as ONE persistent card keyed by
        step id, never a transient row. Later frames fold into that same card,
        and a case switch resets the registry."""
        card = self._sim_cards.get(step.step_id)
        if card is None:
            # Mirror the gate-card
            # discipline -- close out the current streaming entry BEFORE the
            # card inserts, so post-card narration mints a FRESH entry BELOW
            # it (chronological turn flow; the card never strands at the
            # bottom while text piles above it).
            self._close_pending_for_card()
            card = SimCard(_run_identity(step.engine, step.module))
            self._sim_cards[step.step_id] = card
            self.messages_layout.insertWidget(
                self.messages_layout.count() - 1, card
            )
            self._scroll_to_bottom()
        card.update_from_step(step)

    def _close_pending_for_card(self) -> None:
        """A card is about to insert: close out the streaming entry so later
        narration mints a FRESH one BELOW it, and clear its transient rows so
        they re-render below rather than freezing above the card."""
        if self._pending is not None:
            self._pending.clear_tool_card()
            # This entry receives no more deltas --
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
        """The status text is the ACTIVE MODEL name, since the dot already
        carries connectedness: the Settings pick first, else the agent's probed
        default, else empty."""
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
        """Ask the agent for its env-default model off-thread, so the status
        strip names the real running model when the picker is blank. A NO-OP
        when the user picked one: that value is authoritative."""
        if self.settings.model_id:
            return
        task = _EffectiveModelTask(self._effective_http_base(), self)
        task.finished.connect(self._on_effective_model)
        self._effective_model_tasks.append(task)  # keep-alive
        task.start()

    def _on_effective_model(self, model_id: str) -> None:
        self._effective_model = model_id or ""
        self._refresh_model_label()

    def _on_connected(
        self,
        user_id: str,
        is_anonymous: bool,
        http_base: str = "",
        data_base: str = "",
    ) -> None:
        self._connected = True
        # Remote-daemon (tailnet) endpoint derivation: stash whatever this
        # handshake advertised BEFORE any :8766 call or layer materialize
        # below reads through ``_effective_http_base`` / ``_effective_data_base``.
        self._advertised_http_base = http_base or None
        self._advertised_data_base = data_base or None
        self._configure_store_access()
        self._refresh_model_label()
        self._probe_effective_model()

    def _on_case_ready(self, case_id: str) -> None:
        self._case_id = case_id
        self.materializer.set_case(case_id, self._session_case_title or None)
        self._set_case_label(self._session_case_title or case_id[:8])
        self._set_dot("connected")
        # The status strip carries neither the case-id chip nor
        # a "Connected" word -- the case identity rides the dock TITLEBAR
        # (``_set_case_label``), the green dot means connected, and the status
        # TEXT shows the active model.
        self._refresh_model_label()
        # A case picked from the Cases
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
        """The shared tailnet token was rejected (broker 401/403 or in-band
        AUTH_REQUIRED): the worker has STOPPED -- no silent reconnect loop. Say
        exactly what to do next."""
        self._connected = False
        self._pending_open_case = None  # the connect this was riding died
        self._set_dot("error")
        self.status_label.setText(
            "Token rejected -- check the shared token in Settings"
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
        # No case-id chip
        # and no "Reconnected" word in the signifier -- the green dot means
        # connected; the status text returns to the active model name.
        self._refresh_model_label()

    def _on_event(self, kind: str, data: object) -> None:
        if not isinstance(data, dict):
            return
        # picker fail-open: any turn-progress event proves the server
        # already resolved every still-open tool picker (its timeout_s
        # proceeded, or the turn errored/completed past it) -- fold them to
        # the "agent proceeded" chip BEFORE handling the event. A new
        # tool-candidates request supersedes older open pickers too (the
        # server asks one selection at a time; moving to the next wave means
        # the previous one is settled).
        if kind in _PICKER_SUPERSEDE_KINDS or kind == "tool-candidates":
            self._supersede_open_tool_pickers()
        if kind == "thinking-chunk":
            # Local model reasoning-channel token.
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
            # Assemble the parent tool card's inner
            # rows + the ONE bottom metadata block. Each inner row is just a
            # tool label + its state (the card renders ">" + label + a status
            # glyph); the per-row arg and state detail is folded into the
            # muted metadata block at the bottom of the card instead.
            steps = data.get("steps") or []
            inner_rows: List[dict] = []
            meta_lines: List[str] = []
            for step in steps:
                if not isinstance(step, PipelineStep):
                    continue
                if step.tool_name.lower() in _LLM_STEP_NAMES:
                    continue
                if step.role == "compute":
                    # The off-box solver
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
                # The arg summary
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
                    # One collapsed
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
            # A live mid-turn chart lands in the
            # bottom-docked ChartsWindow (never a chat widget). The chat gets
            # only the incremented "Charts (N)" button + one pointer note so
            # the turn's narrative says where the chart went.
            window = self._ensure_charts_window()
            if window.add_chart(data):
                self._charts_count = window.count
                self._set_charts_button(flag=True)
                title = data.get("title") or "chart"
                self._ensure_pending().add_note(
                    f"Chart added to the charts window: {title}"
                )
                self._scroll_to_bottom()
        elif kind == "solve-progress":
            # The ~10 s big-sim telemetry
            # tick. It carries run_id, not step_id; the local seam runs one
            # sim at a time, so fold it into every non-terminal SimCard (the
            # card itself run_id-stamps and ignores live-only fields once
            # terminal -- a straggler tick never repaints a finished card).
            for card in self._sim_cards.values():
                if not card.terminal:
                    card.update_from_progress(data)
        elif kind == "tool-io":
            # Raw-args sidecar keyed by
            # step_id (emitted at dispatch START, so the summary is in place
            # before the pipeline frame paints the chip row).
            sid = data.get("step_id")
            raw = data.get("raw_args")
            if isinstance(sid, str) and sid and isinstance(raw, str):
                self._tool_args_by_step[sid] = _short_args_summary(raw)
        elif kind == "payload-warning":
            self._show_gate_card(data)
        elif kind == "code-exec-request":
            # The code-exec HARD confirm gate: the agent BLOCKS until the
            # reply lands, so this envelope must never be dropped.
            self._show_code_exec_card(data)
        elif kind == "credential-request":
            # The key prompt for a PAUSED keyed tool. The pause has a
            # server-side TTL, so an undelivered card fails the tool.
            self._show_credential_card(data)
        elif kind == "tool-candidates":
            # The tool-selection picker: ranked candidates, free text and
            # let-agent-decide, replying on ONE envelope. FAIL-OPEN --
            # unanswered, the server proceeds and the hook above folds it.
            self._show_tool_candidates_card(data)
        elif kind == "region-choice-request":
            # A gate WAIT: the server snapped a vague geocode to the whole
            # state and PAUSES the turn on the reply. A whole-state answer
            # keeps the honest default, so the gate always closes.
            self._show_region_choice_card(data)
        elif kind == "spatial-input-request":
            # A gate WAIT: the agent needs a picked geometry and PAUSES the
            # turn. The card is wired to the canvas point and AOI tools, and
            # Cancel closes the gate.
            self._show_spatial_input_card(data)
        elif kind == "processing-request":
            # A gate WAIT: the agent asks THIS session to run an algorithm or
            # an approved snippet and PAUSES until the response lands, so the
            # envelope is answered here, on the GUI thread, never dropped.
            self._on_processing_request(data)
        elif kind == "secrets-list":
            # The per-user/per-Case secret roster --
            # store it for the settings/secrets state (minimal honest
            # handling; raw keys never ride here).
            self._on_secrets_list(data)
        elif kind == "case-open":
            self._on_case_open_event(data)
        elif kind == "case-list":
            cases = data.get("cases")
            if isinstance(cases, list):
                self._cases = [c for c in cases if isinstance(c, CaseInfo)]
                if self._cases_dialog is not None:
                    self._cases_dialog.set_cases(self._cases)
        elif kind == "raw" and data.get("type") == "map-command":
            # The agent frames a mesh preview
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
                # The answer text is final -- convert
                # the plain streamed text to rendered markdown now.
                self._pending.finalize_markdown()
                self._pending = None
        else:
            # Catch-all: log unhandled envelope kinds so new server-emitted
            # types are visible in the QGIS log rather than silently dropped
            # (the code-exec stall and loop_exhausted bugs were both caused
            # by silent drops here).
            raw_type = data.get("type", kind) if isinstance(data, dict) else kind
            try:
                from qgis.core import QgsMessageLog, Qgis  # type: ignore[import-not-found]
                QgsMessageLog.logMessage(
                    f"unhandled envelope kind={kind!r} type={raw_type!r}",
                    "TRID3NT",
                    Qgis.Warning,
                )
            except Exception:  # noqa: BLE001 -- headless/test: no QGIS
                pass

    def _on_case_open_event(self, payload: dict) -> None:
        """A ``case-open`` rehydration arrived: rebind the dock to that case
        -- title, a FRESH layer group, its persisted layers streamed in place,
        the basemap, and the zoom. ONE gesture restores chat AND layers."""
        # The zoom runs LAST and UNCONDITIONALLY on every path that reaches
        # this handler -- select, New case, and the startup-reuse select alike.
        # Nothing above it may skip or short-circuit past it, or a case opens
        # with the canvas left wherever it happened to be.
        info = parse_case_open(payload)
        if info is None:
            self._note(
                "Case switch failed: the server could not rehydrate the case "
                "(archived/deleted between list and select?)",
                error=True,
            )
            return
        self._case_id = info.case_id
        # The previous case's bubbles/notes/gate cards must not
        # survive a switch -- clear the message list before repainting
        # anything for the newly-opened case.
        self._clear_messages()
        self.materializer.set_case(info.case_id, info.title)
        self._set_case_label(info.title)
        self._set_dot("connected")
        self._refresh_model_label()  # status text = active model, not case-id
        self._replay_chat_history(info.chat_messages)
        # Layer restore rides the SAME gesture
        # as the chat replay above -- the by-URI materializer reads the store
        # directly (MinIO on this box or the tailnet peer).
        if info.layers:
            notes = self.materializer.materialize(info.layers)
            if notes:
                # Fold the batch rather than painting one chat line per
                # layer, which pushes the conversation far up on a real case.
                # Errors stay visible.
                self._ensure_pending().add_layer_notes(notes)
        # Rebuild the charts window's list from the
        # case's persisted SessionChartRecords (per-case durability). A
        # chart-less case leaves the (possibly-existing) window empty and the
        # button at "Charts (0)"; a chart-carrying case builds the window
        # lazily but does NOT force it visible (the button invites it open).
        if info.charts or self._charts_window is not None:
            window = self._ensure_charts_window()
            window.set_charts(info.charts)
            self._charts_count = window.count
        else:
            self._charts_count = 0
        self._set_charts_button()
        if self.settings.auto_basemap:
            note = ensure_basemap(self.settings.basemap_preset)
            if note:
                self._note(note)
        self._zoom_after_case_open(info)
        # Show the just-opened case's persisted AOI
        # as the dashed overlay (the exact bbox the agent references each turn
        # via state.case_bbox), so the user sees + can re-draw it. A bbox-less
        # case leaves the overlay cleared (already reset in _clear_messages).
        self._case_bbox = info.bbox
        if info.bbox is not None:
            self._render_aoi_overlay(info.bbox)
        self._scroll_to_bottom()

    def _zoom_after_case_open(self, info) -> None:
        """Zoom the canvas to the just-opened case's area, SILENTLY. A case
        with nothing to zoom to says so honestly rather than leaving the view
        wherever it was; headless is a no-op."""
        # The ladder, each rung tried only when the one above it is absent or
        # its transform fails: the case's own bbox; the union of the vector
        # layers this open just materialized (an XYZ raster reports a
        # whole-world extent, so rasters never drive it); any bbox elsewhere in
        # the raw payload; the dock's cached case-list row, which is populated
        # independently and can carry one when the rest come up empty.
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- no canvas (headless), nothing to zoom
            return
        if info.bbox is not None and zoom_to_bbox4326(canvas, info.bbox):
            return
        try:
            dest_crs = canvas.mapSettings().destinationCrs()
        except Exception:  # noqa: BLE001
            return
        extent = self.materializer.last_added_vector_extent(dest_crs)
        if zoom_to_extent(canvas, extent):
            return
        fallback = self._fallback_case_bbox(info)
        if fallback is not None and zoom_to_bbox4326(canvas, fallback):
            return
        self._note("Case has no stored map area - keeping current view")

    def _fallback_case_bbox(
        self, info
    ) -> Optional[Tuple[float, float, float, float]]:
        """A bbox from OUTSIDE the case row: the raw payload first, then the
        cached case-list row. The first usable EPSG:4326 bbox, or None; never
        raises."""
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

    # -- charts window --------------------------------------------------------- #

    def _ensure_charts_window(self) -> ChartsWindow:
        """Lazily build the bottom-docked charts window, on the first chart or
        the first button click. Headless it stays a standalone widget, still
        fully driveable."""
        if self._charts_window is None:
            self._charts_window = ChartsWindow(
                locate_callback=self._locate_layer_on_map,
                parent=self,
            )
            try:
                self.iface.addDockWidget(
                    Qt.DockWidgetArea.BottomDockWidgetArea, self._charts_window
                )
            except Exception:  # noqa: BLE001 -- headless / no main window
                pass
        return self._charts_window

    def _show_charts_window(self) -> None:
        """The chat "Charts (N)" button: create-if-needed + raise the bottom
        window, clearing the new-chart flag (the count stays)."""
        window = self._ensure_charts_window()
        window.setVisible(True)
        try:
            window.raise_()
        except Exception:  # noqa: BLE001 -- headless
            pass
        self._set_charts_button(flag=False)

    def _set_charts_button(self, flag: bool = False) -> None:
        """Repaint the "Charts (N)" button: the count, plus a subtle "*" flag
        when a new chart arrived while the window was not being looked at
        (cleared when the user opens/raises the window)."""
        star = " *" if flag else ""
        self.charts_btn.setText(f"Charts ({self._charts_count}){star}")

    def _locate_layer_on_map(self, source_uri: str) -> None:
        """Pan and flash the canvas to the layer a chart was computed from.
        An unmatched uri is an HONEST note, never a silent no-op; headless is
        a no-op."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- no canvas (headless)
            return
        layer = self._find_layer_by_source_uri(source_uri)
        if layer is None:
            self._note(
                "This chart's source layer is not loaded in QGIS - open it "
                f"first (source: {source_uri})"
            )
            return
        try:
            from qgis.core import (
                QgsCoordinateTransform,
                QgsGeometry,
                QgsProject,
            )

            extent = layer.extent()
            dest_crs = canvas.mapSettings().destinationCrs()
            src_crs = layer.crs()
            if src_crs != dest_crs:
                transform = QgsCoordinateTransform(
                    src_crs, dest_crs, QgsProject.instance().transformContext()
                )
                extent = transform.transformBoundingBox(extent)
            zoom_to_extent(canvas, extent)
            # Best-effort flash so the eye catches the located layer.
            try:
                canvas.flashGeometries(
                    [QgsGeometry.fromRect(extent)], dest_crs
                )
            except Exception:  # noqa: BLE001 -- flash is optional chrome
                pass
            self._note(f"Located '{layer.name()}' on the map")
        except Exception as exc:  # noqa: BLE001
            self._note(
                f"Could not locate the source layer on the map "
                f"({type(exc).__name__}: {exc})",
                error=True,
            )

    def _find_layer_by_source_uri(self, source_uri: str):
        """A loaded layer matching ``source_uri``: the exact stamp first, else
        a substring match either way, because two uris for one object can
        differ in scheme or host while sharing the key. First match or None."""
        try:
            from qgis.core import QgsProject

            layers = list(QgsProject.instance().mapLayers().values())
        except Exception:  # noqa: BLE001 -- headless / no project
            return None
        # Pass 1: exact stamp match (the reliable path for our materialized
        # layers).
        for layer in layers:
            try:
                if layer.customProperty("trid3nt/source_uri") == source_uri:
                    return layer
            except Exception:  # noqa: BLE001
                continue
        # Pass 2: substring either direction against the provider source.
        for layer in layers:
            try:
                src = layer.source() or ""
            except Exception:  # noqa: BLE001
                continue
            if src and (src in source_uri or source_uri in src):
                return layer
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
        # One envelope, two renderers, chosen by what the payload CARRIES: a
        # param sheet is an editable property grid, not a paragraph of
        # provenance text.
        sheet = gate.parse_param_sheet(payload)
        if sheet is not None:
            card = FormCard(warning, sheet, self._on_gate_decision)
        else:
            card = GateCard(warning, self._on_gate_decision)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        # Without this a card strands at the bottom while the response text
        # piles above it: the
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

    # -- code-exec approval card ----------------------------------------------- #

    def _show_code_exec_card(self, payload: dict) -> None:
        """Render the code-exec HARD confirm gate as an inline approval card.
        The agent does not send the code until the decision rides back, so a
        malformed envelope is noted honestly rather than dropped."""
        request = gate.parse_code_exec_request(payload)
        if request is None:
            self._note(
                "Received a malformed code-exec-request (no code_exec_id / "
                "python_code) -- cannot approve it; the agent's confirm gate "
                "will time out server-side.",
                error=True,
            )
            return
        card = CodeExecCard(request, self._on_code_exec_decision)
        # Track the card by code_exec_id so the processing-request that follows
        # the approval folds its outcome into THIS card's chip.
        self._code_exec_cards[request.code_exec_id] = card
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._close_pending_for_card()
        self._scroll_to_bottom()

    def _on_code_exec_decision(self, code_exec_id: str, decision: str) -> None:
        """Send the code-exec confirmation. The reply REUSES the payload
        confirmation envelope rather than adding an outbound verb, and
        ``revised_args`` is always None."""
        try:
            self.bridge.confirm_payload(code_exec_id, decision, None)
        except Exception as exc:  # noqa: BLE001
            self._note(f"code-exec confirmation send failed: {exc}", error=True)

    def _on_processing_request(self, payload: dict) -> None:
        """Run the request in this session and answer it; a code request also
        folds its outcome into the card that approved it. The agent is paused
        on the reply, so an exception here still answers with its message."""
        response = run_processing_request(payload, iface=self.iface)
        try:
            self.bridge.send_processing_response(**response)
        except Exception as exc:  # noqa: BLE001
            self._note(f"processing response send failed: {exc}", error=True)
        code_exec_id = payload.get("code_exec_id")
        card = self._code_exec_cards.get(code_exec_id) if isinstance(code_exec_id, str) else None
        if card is None:
            return
        outcome = gate.CodeExecResult(
            code_exec_id=code_exec_id,
            status=str(response.get("status") or "error"),
            stdout_tail=str(response.get("stdout") or ""),
            stderr_tail=str(response.get("error") or ""),
            result=response.get("result") if isinstance(response.get("result"), dict) else None,
        )
        try:
            card.update_from_result(outcome)
        except RuntimeError:
            # The underlying C++ widget died (case switch raced the result).
            self._code_exec_cards.pop(code_exec_id, None)

    # -- secrets-list roster (settings/secrets state) --------------------------- #

    def _on_secrets_list(self, payload: dict) -> None:
        """Store the ``secrets-list`` roster as the durable state the settings
        dialog reads. Raw keys NEVER ride this envelope, and a one-line count
        note lands in chat so the refresh is visible."""
        self._secrets = gate.parse_secrets_list(payload)
        active = [s for s in self._secrets if getattr(s, "is_active", True)]
        if active:
            self._ensure_pending().add_note(
                f"Saved API keys: {len(active)} "
                + "(" + ", ".join(s.display for s in active[:6])
                + (", ..." if len(active) > 6 else "") + ")"
            )
            self._scroll_to_bottom()

    # -- region-choice picker card (a gate WAIT) -------------------------------- #

    def _show_region_choice_card(self, payload: dict) -> None:
        """Render the region-choice gate as an inline picker card. A
        malformed envelope cannot be answered, but the server's own default is
        the whole-state bbox, so the note says the turn proceeds with that."""
        request = gate.parse_region_choice(payload)
        if request is None:
            self._note(
                "Received a malformed region-choice-request (no request_id) -- "
                "cannot answer it; the agent will proceed with the whole-state "
                "bbox it already resolved.",
                error=True,
            )
            return
        card = RegionChoiceCard(request, self._on_region_choice_decision)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._close_pending_for_card()
        self._scroll_to_bottom()

    def _on_region_choice_decision(
        self,
        request_id: str,
        choice: str,
        selected_region_id: Optional[str],
        selected_bbox: Optional[list],
    ) -> None:
        """Send the region-choice reply. A whole-state answer keeps the honest
        already-resolved bbox: the decline path that still CLOSES the gate."""
        try:
            self.bridge.send_region_choice(
                request_id,
                choice,
                selected_region_id=selected_region_id,
                selected_bbox=selected_bbox,
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"region choice send failed: {exc}", error=True)

    # -- spatial-input pick card (a gate WAIT) ---------------------------------- #

    def _show_spatial_input_card(self, payload: dict) -> None:
        """Render the spatial-input gate as an inline pick card, wired to the
        SAME canvas machinery the probe and AOI tools use. A malformed envelope
        is noted honestly rather than dropped."""
        request = gate.parse_spatial_input_request(payload)
        if request is None:
            self._note(
                "Received a malformed spatial-input-request (no request_id / "
                "unknown mode) -- cannot answer it; the agent's pick prompt "
                "will time out server-side.",
                error=True,
            )
            return
        default_name = ""
        if request.mode == "point":
            # point-1, point-2, ... for the session: a name the user never edits
            # still tells one picked point from the next.
            self._point_picks = getattr(self, "_point_picks", 0) + 1
            default_name = f"point-{self._point_picks}"
        card = SpatialInputCard(
            request,
            self._on_spatial_input_decision,
            iface=self.iface,
            to_lonlat=self._point_to_lonlat4326,
            to_bbox=self._rect_to_bbox4326,
            default_name=default_name,
        )
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._close_pending_for_card()
        self._scroll_to_bottom()

    def _on_spatial_input_decision(self, wire: dict) -> None:
        """Send the spatial-input reply: coordinates, features, or a cancel.
        A CANCEL still closes the server's paused gate."""
        try:
            self.bridge.send_spatial_input(
                wire["request_id"],
                geometry_type=wire.get("geometry_type"),
                coordinates=wire.get("coordinates"),
                features=wire.get("features"),
                name=wire.get("name"),
                cancelled=bool(wire.get("cancelled")),
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"spatial input send failed: {exc}", error=True)

    # -- credential-request key-entry card -------------------------------------- #

    def _show_credential_card(self, payload: dict) -> None:
        """Render the credential prompt as an inline key-entry card: a keyed
        tool hit a missing key and the agent PAUSED it. A malformed envelope
        is noted honestly rather than dropped."""
        request = gate.parse_credential_request(payload)
        if request is None:
            self._note(
                "Received a malformed credential-request (no request_id / "
                "provider_id) -- cannot answer it; the agent's key prompt "
                "will time out server-side.",
                error=True,
            )
            return
        card = CredentialCard(request, self._on_credential_decision)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._close_pending_for_card()
        self._scroll_to_bottom()

    def _on_credential_decision(
        self, request_id: str, provider_id: str, key_value: Optional[str]
    ) -> None:
        """Send the credential reply: a key submits, ``None`` skips. The raw
        key rides ``secret-add`` ALONE and is never logged or echoed in an
        error note; a skip saves nothing and lets the tool's error stand."""
        try:
            if key_value is None:
                self.bridge.decline_credential(request_id)
            else:
                self.bridge.submit_credential(request_id, provider_id, key_value)
        except Exception as exc:  # noqa: BLE001
            # exc carries transport state, never the key (the send serializes
            # before any failure surface we format here).
            self._note(f"credential reply send failed: {exc}", error=True)

    # -- tool-selection picker card

    def _show_tool_candidates_card(self, payload: dict) -> None:
        """Render the tool-candidates picker as an inline card. FAIL-OPEN: a
        malformed or unanswered request costs only the chance to redirect,
        because the server proceeds with its own pick regardless."""
        request = gate.parse_tool_candidates(payload)
        if request is None:
            self._note(
                "Received a malformed tool-candidates request (no request_id)"
                " -- cannot answer it; the agent will proceed with its own "
                "pick after its timeout.",
                error=True,
            )
            return
        # Increment the per-turn step counter BEFORE
        # constructing the card so a wave of N pickers in one turn reads
        # "Step 1", "Step 2", ... in arrival order.
        self._tool_picker_turn_step += 1
        card = ToolCandidatesCard(
            request, self._on_tool_choice, step_index=self._tool_picker_turn_step
        )
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._open_tool_pickers.append(card)
        self._close_pending_for_card()
        self._scroll_to_bottom()

    def _on_tool_choice(
        self, request_id: str, tool_name: Optional[str], free_text: Optional[str]
    ) -> None:
        """Send the picker reply: ONE ``tool-choice`` envelope carrying the
        verbatim pick, the typed guidance, or both-None (let the agent
        decide)."""
        try:
            self.bridge.send_tool_choice(
                request_id, tool_name=tool_name, free_text=free_text
            )
        except Exception as exc:  # noqa: BLE001
            self._note(f"tool choice send failed: {exc}", error=True)

    def _supersede_open_tool_pickers(self) -> None:
        """Fold every still-open picker to its "agent proceeded" chip, the
        turn having moved on, and drop terminal cards from the list. An
        ANSWERED card is only collected here, never re-folded."""
        if not self._open_tool_pickers:
            return
        for card in self._open_tool_pickers:
            try:
                card.mark_superseded()
            except RuntimeError:
                # The underlying C++ widget died (case switch raced an
                # event) -- nothing to fold; the ref is dropped below.
                pass
        self._open_tool_pickers = [
            c for c in self._open_tool_pickers if not self._picker_terminal(c)
        ]

    @staticmethod
    def _picker_terminal(card: ToolCandidatesCard) -> bool:
        """True when the card reached ANY terminal state (answered or
        superseded) OR its widget already died -- either way it needs no
        further tracking. Never raises."""
        try:
            return card.answered
        except RuntimeError:
            return True

    # -- sending ------------------------------------------------------------- #

    def _send_run_invocation(self, text, run_inv) -> None:
        """Handle a parsed ``!run`` invocation. Help and a local parse error
        render locally and send NOTHING; a valid one echoes the signature as
        the user bubble and dispatches the invocation."""
        self.input_edit.clear()
        self._add_user_bubble(text)
        if run_inv.help:
            self._ensure_pending().add_note(_RUN_USAGE_HINT)
            self._scroll_to_bottom()
            self._pending = None
            return
        if run_inv.error is not None:
            entry = self._ensure_pending()
            entry.add_note(run_inv.error, error=True)
            self._scroll_to_bottom()
            self._pending = None
            return
        # Mirror the chat path's pre-send turn bookkeeping so the tool card +
        # turn-complete land on a fresh streaming entry.
        self._tool_picker_turn_step = 0
        if self._pending is not None:
            self._pending.finalize_markdown()
        self._pending = _AssistantEntry(self.messages_layout)
        self._scroll_to_bottom()
        try:
            self.bridge.send_dev_tool_invoke(
                run_inv.name, run_inv.args, raw_text=text
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"!run send failed: {exc}", error=True)

    def _send(self) -> None:
        # Multi-line composer -- read the
        # full document (toPlainText), not the one-line QLineEdit .text().
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        if not (self._case_id and self.bridge.running):
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        # !run direct tool invocation: PARSE-FIRST, before the chat
        # path. A ``!run`` prefix routes straight to the server as a structured
        # ``dev-tool-invoke``; anything else (including a message that merely
        # MENTIONS !run mid-sentence) returns None and flows to chat below,
        # byte-identically.
        run_inv = parse_run_invocation(text)
        if run_inv is not None:
            self._send_run_invocation(text, run_inv)
            return
        self.input_edit.clear()
        self._add_user_bubble(text)
        # A fresh turn starts a fresh "Step N" count
        # -- any still-open pickers from the PRIOR turn are already folded by
        # _supersede_open_tool_pickers (a picker never survives a
        # turn-complete), so this is safe to reset unconditionally.
        self._tool_picker_turn_step = 0
        if self._pending is not None:
            # Defensive: a WS drop can strand a
            # streaming entry that never saw turn-complete -- final-render
            # it before minting the new turn's entry.
            self._pending.finalize_markdown()
        self._pending = _AssistantEntry(self.messages_layout)
        self._scroll_to_bottom()
        bbox, _source = self._aoi_for_send()
        # The AOI rides the
        # ``aoi_bbox`` payload field now -- the bracketed in-text prose line
        # ("[QGIS map canvas AOI ...]") is GONE; the chat text goes out CLEAN.
        # There is no per-send AOI note: the
        # transcript note fires only WHEN the user sets/changes the AOI
        # (``_on_aoi_extent_chosen``), never as a standing restated readout.
        try:
            # Pass show_thinking so the server enables reasoning-channel
            # forwarding for this turn (local mode only; remote ignores the field).
            # Ride the picked
            # model_id (empty = agent env default) so a MODEL switch applies
            # live on the next message with no agent restart -- mirrors the
            # show_thinking add. Provider base_url/key stay agent-process env.
            # Ride the persisted Auto/Ask
            # mode so the server surfaces tool selection as picker cards in
            # ask mode -- the same per-turn settings carrier as show_thinking
            # /model_id ("auto" is omitted on the wire; it IS the default).
            self.bridge.send_chat(
                text,
                show_thinking=self.settings.show_thinking,
                model_id=self.settings.model_id,
                aoi_bbox=bbox,
                tool_choice_mode=self.settings.tool_choice_mode,
                # attach any pending drawn region to THIS turn, then
                # clear it below (one rectangle, one turn).
                drawn_geometry=self._drawn_region,
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"send failed: {exc}", error=True)
        # Clear-on-send: the drawn region rides exactly one message.
        if self._drawn_region is not None:
            self._clear_region_overlay()

    # -- teardown ------------------------------------------------------------- #

    def shutdown(self) -> None:
        # Unhook the layer-tree push
        # actions so a plugin reload never stacks duplicate menu entries.
        for action in self._push_tree_actions:
            try:
                self.iface.removeCustomActionForLayerType(action)
            except Exception:  # noqa: BLE001
                pass
        self._push_tree_actions = []
        # The bottom charts window is a sibling dock
        # this dock owns -- tear it down with the dock so a plugin reload never
        # leaves an orphaned bottom dock behind.
        if self._charts_window is not None:
            try:
                self.iface.removeDockWidget(self._charts_window)
            except Exception:  # noqa: BLE001 -- headless / never docked
                pass
            self._charts_window.deleteLater()
            self._charts_window = None
        self.bridge.stop()
        # Remote-streaming session TTL: dock close / plugin unload
        # ends the session -- remove its staging dir so nothing survives it.
        self.materializer.cleanup_session()
