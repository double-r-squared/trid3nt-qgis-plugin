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

from qgis.core import QgsMapLayer

from . import draw_tools, gate
from .charts_window import ChartsWindow
from .library_window import LibraryWindow
from .cards import (
    CodeExecCard,
    FormCard,
    GateCard,
    SimCard,
    SpatialInputCard,
    ToolCandidatesCard,
    _AssistantEntry,
    _ChatInput,
    _ToolCard,
    _WrapLabel,
    run_identity,
    tool_row,
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
    _LibrarySearchTask,
    _LibraryTask,
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
from ..render.layer_request import run_layer_request
from ..render.processing import run_processing_request
from ..render.layers import (
    LayerMaterializer,
    configure_store_access,
    ensure_basemap,
    reproject,
    sweep_stale_session_dirs,
    zoom_to_bbox4326,
    zoom_to_extent,
)


# LLM bookkeeping step names the dock hides from the tool timeline (``model_generate`` is the server's model-stream step).
_LLM_STEP_NAMES = {
    "llm_generation", "thinking", "llm",
    "model_generate", "generate",
}

# Event kinds that mean the TURN MOVED ON: arriving while a ToolCandidatesCard is unanswered proves
# the server's fail-open already resolved it. Housekeeping kinds (session-state / case-list /
# solve-progress) are absent because they can arrive while the server still waits on the pick.
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
# The coloured DOT alone signifies connection state; the titlebar carries the case name.

_USER_BUBBLE_STYLE = (
    "background-color: #1f6feb; color: white; border-radius: 8px; padding: 6px 9px;"
)


def _short_args_summary(raw_args: str, max_len: int = 64) -> str:
    """A compact ``k=v`` arg summary for a tool row: the first three keys, each value clipped,
    nested structures collapsed. This is a SUMMARY, not an IO dump; unusable input yields ""
    and the row renders without args."""
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
        # Only DEAD owners are swept; a concurrent live QGIS keeps its staging dir.
        sweep_stale_session_dirs()
        self.materializer = LayerMaterializer(self.settings)
        self._pending: Optional[_AssistantEntry] = None
        self._connected = False
        # Advertised by the last connect handshake (None until a daemon advertises them); every :8766
        # and store caller resolves through ``_effective_http_base`` / ``_effective_data_base``.
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
        self._provider_config_tasks: List[_ProviderConfigTask] = []
        # Built lazily (QgsMapToolEmitPoint needs a live canvas); ``_prev_map_tool`` is restored on toggle-off.
        self._probe_map_tool = None
        self._prev_map_tool = None
        # Per-case AOI: ``_case_bbox`` (EPSG:4326 ``(w, s, e, n)``) and its overlay ``_aoi_rubber``,
        # CLEARED on case switch and disconnect so a stale box never lingers.
        self._case_bbox: Optional[Tuple[float, float, float, float]] = None
        self._aoi_rubber = None
        self._aoi_map_tool = None
        self._prev_aoi_tool = None
        # ONE rubber-band rectangle rides the NEXT chat turn as ``drawn_geometry``; CLEARED on send.
        self._drawn_region: Optional[dict] = None
        self._region_rubber = None
        self._region_map_tool = None
        self._prev_region_tool = None
        self._aoi_key_filter_on = False
        self._refresh_debounce = Debouncer()
        # A case picked before/while connecting, opened once connect completes.
        self._pending_open_case: Optional[Tuple[str, str]] = None
        self._auto_connect_done_this_show = False
        self._sim_cards: Dict[str, SimCard] = {}
        self._tool_args_by_step: Dict[str, str] = {}
        # Still-open picker cards in arrival order; a later turn event folds them to "agent proceeded".
        self._open_tool_pickers: List[ToolCandidatesCard] = []
        self._code_exec_cards: Dict[str, CodeExecCard] = {}
        self._secrets: list = []
        # 1-based picker-card counter behind "Step N": arrival order, reset on every user turn and case switch.
        self._tool_picker_turn_step = 0
        # Charts get their OWN bottom-docked window, built LAZILY.
        self._charts_window: Optional[ChartsWindow] = None
        # The Library is a sibling bottom window, built LAZILY, read-only and case-independent.
        self._library_window: Optional[LibraryWindow] = None
        self._library_tasks: List[object] = []

        self._build_ui()
        self._wire_bridge()
        self._configure_store_access()
        self._push_tree_actions: List = []
        self._register_layer_tree_push_action()

    def showEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        super().showEvent(event)
        self._auto_connect_local_once()

    def hideEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        super().hideEvent(event)
        self._auto_connect_done_this_show = False

    def _auto_connect_local_once(self) -> None:
        """Connect without the user pressing Connect, ONCE per dock show and reset on hide. It
        never retries within one show: a failure paints the same honest status line a manual
        click would."""
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

        # Status strip: the connection dot (colour is the signifier) + the RUNNING MODEL name.
        status_row = QHBoxLayout()
        self.dot = QLabel()
        self._set_dot("disconnected")
        status_row.addWidget(self.dot)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size: 9pt;")
        status_row.addWidget(self.status_label, 1)
        outer.addLayout(status_row)

        # The agent's effective model id (picker override, else the env default probed on connect).
        self._effective_model: str = ""
        self._effective_model_tasks: List["_EffectiveModelTask"] = []

        # Connect and disconnect live in Settings; the dock auto-connects.
        button_row = QHBoxLayout()

        def _tool_button(text, signal_owner, tip="", checkable=False):
            btn = QToolButton()
            btn.setText(text)
            if tip:
                btn.setToolTip(tip)
            btn.setCheckable(checkable)
            (btn.toggled if checkable else btn.clicked).connect(signal_owner)
            button_row.addWidget(btn)
            return btn

        self.cases_btn = _tool_button("Cases", self._open_cases)

        # Map-click point probe: ON installs a QgsMapToolEmitPoint saving the active tool; OFF restores it.
        self.probe_btn = _tool_button(
            "Probe", self._toggle_probe_tool,
            "Click the map to sample the case's layers at a point",
            checkable=True)

        # Per-case AOI: drag a rectangle to set the extent the agent references every turn
        # (state.case_bbox). Same save/restore discipline as Probe; guarded in _toggle_aoi_draw.
        self.aoi_btn = _tool_button(
            "Set AOI", self._toggle_aoi_draw,
            "Drag a rectangle on the map to set this case's area of interest",
            checkable=True)

        # 'Draw region': ONE rectangle that rides the NEXT chat turn as ``drawn_geometry``; distinct from Set AOI, cleared on send.
        self.region_btn = _tool_button(
            "Draw region", self._toggle_draw_region,
            "Drag a rectangle to attach to your next message as a refinement "
            "region (e.g. where GeoClaw refines its mesh). Cleared on send.",
            checkable=True)

        # Clearing the AOI is multiplexed into the Set-AOI tool: BACKSPACE/DELETE through a canvas eventFilter installed only while ON.

        # Settings is a COG GLYPH so no word label competes with the connection dot.
        button_row.addStretch(1)  # push Settings to the right end of the row
        self.settings_btn = _tool_button(
            "\u2699",  # gear glyph (cog); icon-only look
            self._open_settings,
            "Settings (connect, charts and the library live here)")
        outer.addLayout(button_row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # The transcript NEVER grows a horizontal scrollbar: error text must reflow; wrapped labels cap their minimum width to 1.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.messages_host = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_host)
        self.messages_layout.setContentsMargins(2, 2, 2, 2)
        self.messages_layout.setSpacing(4)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_host)
        outer.addWidget(self.scroll, 1)

        # Probe output renders in this collapsible panel under the message list, REPLACED in place
        # on each click; nothing probe-related enters the chat list. Hidden until the first probe.
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

        # The charts surface is the bottom-docked window; this dock keeps only the count button.
        # The only AOI note is the one-time line emitted when the user sets one.
        # ENTER sends and SHIFT+ENTER newlines.
        input_row = QHBoxLayout()
        self.input_edit = _ChatInput(self._send)
        self.input_edit.setPlaceholderText("Ask for data or a simulation...")
        input_row.addWidget(self.input_edit, 1)
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
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setStyleSheet(_USER_BUBBLE_STYLE)
        # The bare QSizePolicy(h, v) ctor DROPS QLabel.setWordWrap's height-for-width flag, which
        # clipped long messages; restore it (``_WrapLabel`` covers layout paths that ignore it).
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
        """Remove every message-list child widget while KEEPING the terminal stretch item,
        which every other insertion goes before. Drops the pending streaming entry: a stale
        target must never take new deltas."""
        self._pending = None
        # Every registry below tracks widgets the loop at the bottom deletes; drop the refs too.
        self._sim_cards.clear()
        self._tool_args_by_step.clear()
        self._open_tool_pickers = []
        self._tool_picker_turn_step = 0
        self._code_exec_cards.clear()
        self._secrets = []
        # AOI and region overlays live on the canvas, so they need their own clear.
        self._clear_aoi_overlay()
        self._clear_region_overlay()
        self._probe_panel.setVisible(False)
        self.probe_result_label.setText("")
        # The charts window SURVIVES the switch; only its list resets.
        if self._charts_window is not None:
            self._charts_window.clear()
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _replay_chat_history(self, messages: List[dict]) -> None:
        """Repaint a just-opened case's persisted conversation."""
        tool_group: List[dict] = []
        for row in messages:
            role = row.get("role")
            if role == "tool":
                card = row.get("tool_card")
                if isinstance(card, dict):
                    tool_group.append(card)
                continue
            if tool_group:
                self._replay_tool_group(tool_group)
                tool_group = []
            content = row.get("content")
            # A persisted terminal-error row replays as a wrapped inline error line; consecutive ones fold like live arrival.
            if role == "error" or row.get("is_error"):
                if isinstance(content, str) and content:
                    self._note(content, error=True)
                continue
            if not isinstance(content, str) or not content:
                continue
            # A conversational row ends any open error run so separated errors never glue into one fold.
            if self._pending is not None:
                self._pending.break_error_run()
            if role == "user":
                self._add_user_bubble(content)
            elif role == "agent":
                entry = _AssistantEntry(self.messages_layout)
                # Replay persisted reasoning as the same collapsed thinking fold the live path builds
                # ("reasoning" stays as a defensive alias of "thinking").
                thinking = row.get("thinking") or row.get("reasoning")
                if isinstance(thinking, str) and thinking.strip():
                    entry.append_thinking_delta(thinking)
                    entry.collapse_thinking()
                entry.append_delta(content)
                entry.finalize_markdown()
        if tool_group:
            self._replay_tool_group(tool_group)

    def _replay_tool_group(self, cards: List[dict]) -> None:
        """Render a run of persisted tool rows as ONE parent card, each row carrying its
        response as a collapsed read-only body."""
        inner_rows: List[dict] = []
        meta_lines: List[str] = []
        for card in cards:
            name = card.get("tool_name") or card.get("label") or "tool"
            raw_args = card.get("raw_args")
            inner_rows.append(tool_row(
                name, card.get("state"),
                result=card.get("function_response"),
                is_error=bool(card.get("is_error")),
            ))
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

    def _rect_to_bbox4326(
        self, extent, authid: str
    ) -> Optional[Tuple[float, float, float, float]]:
        """A QgsRectangle in ``authid`` -> an EPSG:4326 bbox tuple, or None. Never raises: an
        unresolvable CRS yields None and the status line says so honestly."""
        bbox = aoi.extent_to_bbox4326(
            extent.xMinimum(), extent.yMinimum(),
            extent.xMaximum(), extent.yMaximum(), authid,
        )
        if bbox is not None:
            return bbox
        rect = reproject(extent, authid)
        if rect is None:
            return None
        return aoi.extent_to_bbox4326(
            rect.xMinimum(), rect.yMinimum(),
            rect.xMaximum(), rect.yMaximum(), "EPSG:4326",
        )

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
        """The active layer's SELECTION bbox as EPSG:4326, or None. The bbox OF the selection,
        never its ring: every AOI carrier on the wire is a four-number box."""
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

    def _aoi_for_send(self) -> Optional[Tuple[float, float, float, float]]:
        """The bbox to attach right now, or None."""
        bbox = self._case_bbox
        if bbox is not None and aoi.bbox_within_guard(bbox):
            return bbox
        return None

    def _point_to_lonlat4326(
        self, point, authid: str
    ) -> Optional[Tuple[float, float]]:
        """A clicked ``QgsPointXY`` in ``authid`` -> EPSG:4326 ``(lon, lat)``, or None. Never
        raises: an unresolvable CRS or an out-of-range result yields None and the click note
        says so."""
        import math

        authid_norm = (authid or "").strip().upper()
        x, y = point.x(), point.y()
        if authid_norm in ("EPSG:4326", "OGC:CRS84"):
            lon, lat = x, y
        elif authid_norm == "EPSG:3857":
            lon, lat = aoi.merc_to_lonlat(x, y)
        else:
            transformed = reproject(point, authid)
            if transformed is None:
                return None
            lon, lat = transformed.x(), transformed.y()
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return None
        return lon, lat

    def _toggle_probe_tool(self, checked: bool) -> None:
        """Install or restore the canvas map tool for the Probe button. ON SAVES whatever tool
        was active first, and OFF restores it, so the canvas is never left on a tool the
        user did not ask for."""
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
        self._prev_map_tool = draw_tools.borrow_map_tool(
            canvas, self._probe_map_tool, checked, self._prev_map_tool)

    def _toggle_probe_results(self) -> None:
        self.probe_result_label.setVisible(
            self.probe_results_toggle.isChecked()
        )

    def _set_probe_output(self, text: str, error: bool = False) -> None:
        """The latest probe status, result or error goes to the PINNED panel under the message
        list, replaced in place on each click -- never a chat note."""
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

    def _render_aoi_overlay(
        self, bbox4326: Tuple[float, float, float, float]
    ) -> None:
        """Paint the case AOI as a DASHED, outline-only rectangle, so it reads as an EXTENT and
        not a filled feature."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no iface -- no overlay
            return
        self._aoi_rubber = draw_tools.paint_bbox_overlay(
            canvas, self._aoi_rubber, bbox4326, "#58a6ff", dotted=True)

    def _clear_aoi_overlay(self) -> None:
        """Drop the case AOI state + hide the overlay -- called on every case switch
        (``_clear_messages``) and disconnect (``disconnect_agent``) so a stale box from the
        previous case never lingers on the canvas."""
        self._case_bbox = None
        draw_tools.clear_bbox_overlay(self._aoi_rubber)

    def _toggle_aoi_draw(self, checked: bool) -> None:
        """Install or restore the canvas map tool for the Set-AOI button, ON saving the active
        tool and OFF restoring it. GUARDED: without a live case and connection the button
        snaps back off with an honest note."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no canvas -- no-op
            return
        if checked:
            if not (self._case_id and self.bridge.running):
                self.status_label.setText(
                    "Not connected -- open a case first to set its AOI"
                )
                # Snap back off WITHOUT re-entering this slot (blockSignals).
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
                    # A press/drag/release rubber-band fallback is deferred: snap off and say so.
                    self._note(
                        "Set AOI is unavailable in this QGIS build "
                        "(QgsMapToolExtent missing).",
                        error=True,
                    )
                    self.aoi_btn.blockSignals(True)
                    self.aoi_btn.setChecked(False)
                    self.aoi_btn.blockSignals(False)
                    return
            self._prev_aoi_tool = draw_tools.borrow_map_tool(
                canvas, self._aoi_map_tool, True, self._prev_aoi_tool)
            # While Set-AOI is ON, BACKSPACE/DELETE clears the AOI via a canvas eventFilter removed in the OFF branch.
            canvas.installEventFilter(self)
            self._aoi_key_filter_on = True
        else:
            self._prev_aoi_tool = draw_tools.borrow_map_tool(
                canvas, self._aoi_map_tool, False, self._prev_aoi_tool)
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
        """A rectangle was dragged with Set-AOI: convert it to EPSG:4326, repaint the overlay,
        PERSIST it on the case so every later turn anchors on it, then restore the prior map
        tool."""
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
        # Restore the prior map tool (setChecked(False) runs the OFF branch).
        self.aoi_btn.setChecked(False)

    def _clear_aoi(self) -> None:
        """Clear the current case AOI locally AND server-side, so the agent stops anchoring on
        the old extent; an empty bbox IS the reset."""
        had = self._case_bbox is not None
        self._clear_aoi_overlay()  # hides overlay + nulls self._case_bbox
        if self._case_id and self.bridge.running:
            self.bridge.case_command("set-bbox", self._case_id, {"bbox": None})
            self._note("Case AOI cleared" if had else "No AOI was set")
        else:
            self._note(
                "AOI overlay cleared (not connected -- nothing to sync)"
            )

    def _toggle_draw_region(self, checked: bool) -> None:
        """Install or restore the Draw-region map tool, ON saving the active tool and OFF
        restoring it."""
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
        self._prev_region_tool = draw_tools.borrow_map_tool(
            canvas, self._region_map_tool, checked, self._prev_region_tool)

    def _on_region_extent_chosen(self, rect) -> None:
        """A rectangle was dragged with Draw-region: stash it as the pending drawn geometry and
        paint the overlay."""
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
        """Paint the pending drawn region as a SOLID amber outline, distinct from the dashed
        AOI overlay, so the user sees what will attach."""
        try:
            canvas = self.iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no iface -- no overlay
            return
        self._region_rubber = draw_tools.paint_bbox_overlay(
            canvas, self._region_rubber, bbox4326, "#e3b341")

    def _clear_region_overlay(self) -> None:
        """Drop the pending drawn region + hide its overlay. Called on send (clear-on-send) and
        on case switch / disconnect so a stale region never rides a later turn or lingers on
        the canvas."""
        self._drawn_region = None
        draw_tools.clear_bbox_overlay(self._region_rubber)

    # -- connection ----------------------------------------------------------- #

    def _wire_bridge(self) -> None:
        self.bridge.connected.connect(self._on_connected)
        self.bridge.case_ready.connect(self._on_case_ready)
        # ``agent_event``, never ``event``: that name shadows QObject.event() and qFatals QGIS.
        self.bridge.agent_event.connect(self._on_event)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.closed.connect(self._on_closed)
        self.bridge.reconnecting.connect(self._on_reconnecting)
        self.bridge.resumed.connect(self._on_resumed)
        self.bridge.auth_expired.connect(self._on_auth_expired)

    def _effective_http_base(self) -> str:
        """The resolved agent HTTP base, and the ONE derivation seam every caller of it goes
        through, so they cannot drift apart."""
        return resolve_http_base(self._advertised_http_base, self.settings.local_url)

    def _effective_data_base(self) -> str:
        """The object store's endpoint. Prefers the server-advertised ``data_base``; falls back
        to ``settings.minio_endpoint`` for daemons that do not advertise it."""
        return resolve_data_base(self._advertised_data_base, self.settings.minio_endpoint)

    def _configure_store_access(self) -> None:
        """Point GDAL's ``/vsis3`` at the effective store endpoint."""
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
        # The top-row button only CONNECTS; disabled while a connection is up or in flight.
        title = "QGIS session " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._session_case_title = title
        # A fresh case is BBOX-LESS: the AOI is set explicitly or the agent geocodes it.
        self.bridge.start(
            url,
            token=self.settings.effective_token(),
            case_title=title,
            case_bbox=None,
            # REUSE the resumed or newest case; create only when zero exist.
            reuse_case=True,
        )

    def disconnect_agent(self) -> None:
        self.bridge.stop()
        self._connected = False
        self._case_id = None
        # A disconnect ends the session: sweep everything staged this session.
        self.materializer.cleanup_session()
        # A disconnect ends the case binding, so the overlay goes too.
        self._clear_aoi_overlay()
        self._clear_region_overlay()
        self._set_case_label("")
        self._set_dot("disconnected")
        self.status_label.setText("Not connected")

    def _set_case_label(self, title: str) -> None:
        # An empty title shows the brand word; the dot colour, never the titlebar, signifies connection.
        self._case_title = title
        self.setWindowTitle(title if title else "TRID3NT")

    def _open_settings(self) -> None:
        prev_basemap = self.settings.basemap_preset
        # Hand the dialog the dock's disconnect path and connection state so Disconnect drives the same teardown.
        dlg = SettingsDialog(
            self.settings,
            self,
            on_disconnect=self.disconnect_agent,
            on_connect=self.connect_agent,
            connected=self.bridge.running,
            on_charts=self._show_charts_window,
            on_library=self._show_library_window,
        )
        dlg.exec()
        # The next send recomputes the AOI. An explicit preset change applies here, not gated on
        # auto_basemap (that governs automatic adds).
        if self.settings.basemap_preset != prev_basemap:
            note = ensure_basemap(self.settings.basemap_preset)
            if note:
                self._note(note)

    def _open_cases(self) -> None:
        dlg = CasesDialog(self, self._cases)
        self._cases_dialog = dlg
        # Populate from the cold HTTP route when nothing is shown yet or the live case-list never arrived.
        if not self._cases or not self._connected:
            self._load_cold_case_list(dlg)
        try:
            dlg.exec()
        finally:
            self._cases_dialog = None

    def _load_cold_case_list(self, dlg: "CasesDialog") -> None:
        """Fetch the COLD case list off the UI thread and feed it into ``dlg``, so the dialog
        can populate before any connection exists."""
        dlg.info_lbl.setText("Loading cases ...")
        base_url = self._effective_http_base()
        task = _CaseListTask(base_url, self)
        task.finished.connect(self._on_cold_case_list_finished)
        task.errored.connect(self._on_cold_case_list_errored)
        self._case_list_tasks.append(task)
        task.start()

    def _on_cold_case_list_finished(self, cases: List[CaseInfo]) -> None:
        # A live case-list that landed mid-fetch is authoritative; never clobber it.
        if not self._cases:
            self._cases = cases
        if self._cases_dialog is not None:
            self._cases_dialog.set_cases(self._cases)

    def _on_cold_case_list_errored(self, message: str) -> None:
        """The cold case-list read failed."""
        if self._cases_dialog is not None and not self._cases:
            self._cases_dialog.info_lbl.setText(
                f"Could not read the case list ({message}) - is the local "
                "stack running?"
            )

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
        # A NEW case must never inherit the previous AOI: drop the overlay and null the bbox BEFORE creating.
        self._clear_aoi_overlay()
        self._clear_region_overlay()
        # A NEW case is BBOX-LESS until the user sets one or the model geocodes it.
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
        """Send the ACTIVE QGIS layer into the current case as a first-class input layer."""
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
        """Register the push action on the QGIS layer-tree context menu, one QAction per layer
        type."""
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
        """A compute pipeline step renders as ONE persistent card keyed by step id, never a
        transient row."""
        card = self._sim_cards.get(step.step_id)
        if card is None:
            # Close out the current streaming entry BEFORE the card inserts so later narration mints a FRESH entry BELOW it.
            self._close_pending_for_card()
            card = SimCard(run_identity(step.engine, step.module))
            self._sim_cards[step.step_id] = card
            self.messages_layout.insertWidget(
                self.messages_layout.count() - 1, card
            )
            self._scroll_to_bottom()
        card.update_from_step(step)

    def _close_pending_for_card(self) -> None:
        """A card is about to insert: close out the streaming entry so later narration mints a
        FRESH one BELOW it, and clear its transient rows so they re-render below rather than
        freezing above the card."""
        if self._pending is not None:
            self._pending.clear_tool_card()
            # No more deltas: final-render its markdown before closing it out.
            self._pending.finalize_markdown()
        self._pending = None

    def _note(self, text: str, error: bool = False) -> None:
        self._ensure_pending().add_note(text, error=error)
        self._scroll_to_bottom()

    def _on_provider_config_finished(self, result: dict) -> None:
        """The agent accepted the live provider config (Settings Save): the switch applies on
        the NEXT message with no restart."""
        model = result.get("model") or "the agent default model"
        host = result.get("base_url_host") or ""
        where = f" via {host}" if host else ""
        self._note(
            f"Provider config applied -- {model}{where} applies on your next "
            "message (no restart)."
        )

    def _on_provider_config_errored(self, message: str) -> None:
        """The agent HTTP listener was unreachable or errored on Save: the settings persisted
        but the live push did not land."""
        self._note(
            f"Could not apply the provider config live ({message}) -- "
            "restart the agent to apply the new provider.",
            error=True,
        )

    def _refresh_model_label(self) -> None:
        """The status text is the ACTIVE MODEL name, since the dot already carries
        connectedness: the Settings pick first, else the agent's probed default, else empty."""
        model = (self.settings.model_id or self._effective_model or "").strip()
        if model:
            # Drop any provider prefix ("nvidia/...") but keep the id and a ":free" tag; the full id lives in the tooltip.
            self.status_label.setText(model.split("/")[-1])
            self.status_label.setToolTip(model)
        else:
            self.status_label.setText("")
            self.status_label.setToolTip("")

    def _probe_effective_model(self) -> None:
        """Ask the agent for its env-default model off-thread, so the status strip names the
        real running model when the picker is blank."""
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
        http_base: str = "",
        data_base: str = "",
    ) -> None:
        self._connected = True
        # Stash what this handshake advertised BEFORE any :8766 call or layer materialize reads it.
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
        # Case identity rides the TITLEBAR, the dot means connected, the status text shows the model.
        self._refresh_model_label()
        # A case picked in the Cases dialog while disconnected: the connect created its own fresh case; switch to the requested one.
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
        """The server token was refused (broker 401/403 or in-band AUTH_FAILED): the worker has
        STOPPED -- no silent reconnect loop."""
        self._connected = False
        self._pending_open_case = None  # the connect this was riding died
        self._set_dot("error")
        self.status_label.setText(
            "Token refused -- Add the token under Settings"
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
        # The green dot means connected; the status text returns to the model name.
        self._refresh_model_label()

    def _on_event(self, kind: str, data: object) -> None:
        if not isinstance(data, dict):
            return
        # Any turn-progress event proves the server already resolved every open tool picker (timeout_s
        # proceeded, or the turn errored/completed): fold them to "agent proceeded" BEFORE handling it.
        # A new tool-candidates request supersedes older open pickers too.
        if kind in _PICKER_SUPERSEDE_KINDS or kind == "tool-candidates":
            self._supersede_open_tool_pickers()
        if kind == "thinking-chunk":
            # Local model reasoning token; the block collapses when the first answer delta arrives.
            entry = self._ensure_pending()
            entry.append_thinking_delta(str(data.get("delta") or ""))
            self._scroll_to_bottom()
        elif kind == "chunk":
            entry = self._ensure_pending()
            entry.append_delta(str(data.get("delta") or ""))
            self._scroll_to_bottom()
        elif kind == "pipeline":
            self._on_pipeline(data)
        elif kind == "session-state":
            self._on_session_state(data)
        elif kind == "error":
            code = data.get("error_code") or "ERROR"
            message = data.get("message") or data.get("detail") or ""
            self._ensure_pending().add_note(f"{code}: {message}", error=True)
            self._scroll_to_bottom()
        elif kind == "chart":
            self._on_chart(data)
        elif kind == "solve-progress":
            # The ~10 s big-sim telemetry tick carries run_id, not step_id; the local seam runs one
            # sim at a time, so fold it into every non-terminal SimCard (a terminal card ignores live-only fields).
            for card in self._sim_cards.values():
                if not card.terminal:
                    card.update_from_progress(data)
        elif kind == "tool-io":
            # Raw-args sidecar keyed by step_id, emitted at dispatch START so the summary precedes the chip row.
            sid = data.get("step_id")
            raw = data.get("raw_args")
            if isinstance(sid, str) and sid and isinstance(raw, str):
                self._tool_args_by_step[sid] = _short_args_summary(raw)
        elif kind == "payload-warning":
            self._show_gate_card(data)
        elif kind == "code-exec-request":
            # The code-exec HARD confirm gate: the agent BLOCKS until the reply lands; never drop this envelope.
            self._show_code_exec_card(data)
        elif kind == "tool-candidates":
            # Tool-selection picker, replying on ONE envelope. FAIL-OPEN: unanswered, the server proceeds and the hook above folds it.
            self._show_tool_candidates_card(data)
        elif kind == "spatial-input-request":
            # Gate WAIT: the turn PAUSES until the card is answered or cancelled.
            self._show_spatial_input_card(data)
        elif kind == "processing-request":
            # Gate WAIT: PAUSES until answered, on the GUI thread, never dropped.
            self._on_processing_request(data)
        elif kind == "layer-request":
            # Gate WAIT: PAUSES until answered, on the GUI thread, never dropped.
            self._on_layer_request(data)
        elif kind == "secrets-list":
            # The secret roster is stored for the settings surface; raw keys never ride here.
            self._on_secrets_list(data)
        elif kind == "case-open":
            self._on_case_open_event(data)
        elif kind == "case-list":
            cases = data.get("cases")
            if isinstance(cases, list):
                self._cases = [c for c in cases if isinstance(c, CaseInfo)]
                if self._cases_dialog is not None:
                    self._cases_dialog.set_cases(self._cases)
        elif kind == "map-command":
            self._on_map_command(data)
        elif kind == "turn-complete":
            if self._pending is not None:
                # The answer text is final: render its markdown now.
                self._pending.finalize_markdown()
                self._pending = None
        else:
            # Catch-all: log unhandled envelope kinds so silent drops stay visible.
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


    def _on_pipeline(self, data: dict) -> None:
        """Assemble the parent tool card's inner rows + the ONE bottom metadata block."""
        steps = data.get("steps") or []
        inner_rows: List[dict] = []
        meta_lines: List[str] = []
        for step in steps:
            if not isinstance(step, PipelineStep):
                continue
            if step.tool_name.lower() in _LLM_STEP_NAMES:
                continue
            if step.role == "compute":
                # A compute step renders as ONE persistent SimCard, not an inner row.
                self._route_compute_step(step)
                continue
            if step.tool_name == "context:compact":
                # Compaction narration is a muted metadata line, not a tool row.
                suffix = (
                    f" ({step.substep_label})" if step.substep_label else ""
                )
                meta_lines.append(f"{step.name} - {step.state}{suffix}")
                continue
            inner_rows.append(tool_row(
                step.tool_name or step.name, step.state,
                nested=bool(step.parent_step_id),
            ))
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

    def _on_session_state(self, data: dict) -> None:
        """Materialize this frame's published layers into the case's group."""
        layers = data.get("layers") or []
        if layers and self._case_id:
            notes = self.materializer.materialize(layers)
            if notes:
                # Errors stay visible outside the collapse.
                self._ensure_pending().add_layer_notes(notes)
                self._scroll_to_bottom()

    def _on_chart(self, data: dict) -> None:
        """A live mid-turn chart lands in the bottom-docked ChartsWindow, never a chat widget."""
        window = self._ensure_charts_window()
        if window.add_chart(data):
            title = data.get("title") or "chart"
            self._ensure_pending().add_note(
                f"Chart added to the charts window: {title}"
            )
            self._scroll_to_bottom()

    def _on_map_command(self, data: dict) -> None:
        """Honor ONLY the explicit zoom-to. A preview layer is published role=input with
        bbox=None so it does NOT self-zoom -- the camera must not be yanked for every silent
        input layer."""
        if data.get("command") == "zoom-to":
            bbox = (data.get("args") or {}).get("bbox")
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

    def _on_case_open_event(self, payload: dict) -> None:
        """A ``case-open`` rehydration arrived: rebind the dock to that case -- title, a FRESH
        layer group, its persisted layers streamed in place, the basemap, and the zoom."""
        # The zoom runs LAST and UNCONDITIONALLY on every path that reaches this handler.
        info = parse_case_open(payload)
        if info is None:
            self._note(
                "Case switch failed: the server could not rehydrate the case "
                "(archived/deleted between list and select?)",
                error=True,
            )
            return
        self._case_id = info.case_id
        # Clear the message list BEFORE repainting for the newly-opened case.
        self._clear_messages()
        self.materializer.set_case(info.case_id, info.title)
        self._set_case_label(info.title)
        self._set_dot("connected")
        self._refresh_model_label()  # status text = active model, not case-id
        self._replay_chat_history(info.chat_messages)
        self.input_edit.set_history(
            [r["content"] for r in info.chat_messages if r.get("role") == "user"])
        # Layer restore rides the same gesture: the by-URI materializer reads the store directly.
        if info.layers:
            notes = self.materializer.materialize(info.layers)
            if notes:
                # Fold the batch rather than one chat line per layer; errors stay visible.
                self._ensure_pending().add_layer_notes(notes)
        # Rebuild the charts list from the case's chart records; a chart-carrying case builds the window lazily without forcing it visible.
        if info.charts or self._charts_window is not None:
            self._ensure_charts_window().set_charts(info.charts)
        if self.settings.auto_basemap:
            note = ensure_basemap(self.settings.basemap_preset)
            if note:
                self._note(note)
        self._zoom_after_case_open(info)
        # Show the case's persisted AOI as the dashed overlay; a bbox-less case stays cleared.
        self._case_bbox = info.bbox
        if info.bbox is not None:
            self._render_aoi_overlay(info.bbox)
        self._scroll_to_bottom()

    def _zoom_after_case_open(self, info) -> None:
        """Zoom the canvas to the just-opened case's area, SILENTLY."""
        # The ladder, each rung only when the one above is absent or its transform fails: the case
        # bbox; the union of vector layers just materialized (an XYZ raster reports a whole-world
        # extent); any bbox in the raw payload; the cached case-list row.
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
        """A bbox from OUTSIDE the case row: the raw payload first, then the cached case-list
        row. The first usable EPSG:4326 bbox, or None; never raises."""
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

    def _ensure_charts_window(self) -> ChartsWindow:
        """Lazily build the bottom-docked charts window, on the first chart or the first button
        click."""
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
        """Settings' display-charts entry: create-if-needed + raise the bottom window."""
        window = self._ensure_charts_window()
        window.setVisible(True)
        try:
            window.raise_()
        except Exception:  # noqa: BLE001 -- headless
            pass

    def _ensure_library_window(self) -> LibraryWindow:
        """Lazily build the bottom-docked library window on its first open."""
        if self._library_window is None:
            self._library_window = LibraryWindow(parent=self)
            self._library_window.searchRequested.connect(self._search_library)
            self._library_window.refreshRequested.connect(self._load_library)
            try:
                self.iface.addDockWidget(
                    Qt.DockWidgetArea.BottomDockWidgetArea, self._library_window
                )
            except Exception:  # noqa: BLE001 -- headless / no main window
                pass
        return self._library_window

    def _show_library_window(self) -> None:
        """Settings' search-library entry: create-if-needed, raise, and fetch the listing on
        the first open -- the listing is static for the life of the daemon, so a second open
        re-shows what is already there."""
        window = self._ensure_library_window()
        window.setVisible(True)
        try:
            window.raise_()
        except Exception:  # noqa: BLE001 -- headless
            pass
        if window.group_tree.topLevelItemCount() == 0:
            self._load_library()

    def _load_library(self) -> None:
        window = self._ensure_library_window()
        window.set_status("loading the library ...")
        task = _LibraryTask(self._effective_http_base(), self)
        task.finished.connect(window.set_library)
        task.errored.connect(window.set_status)
        self._library_tasks.append(task)  # keep-alive
        task.start()

    def _search_library(self, query: str) -> None:
        window = self._ensure_library_window()
        task = _LibrarySearchTask(self._effective_http_base(), query, self)
        task.finished.connect(window.set_hits)
        task.errored.connect(window.set_status)
        self._library_tasks.append(task)  # keep-alive
        task.start()

    def _locate_layer_on_map(self, source_uri: str) -> None:
        """Pan and flash the canvas to the layer a chart was computed from. An unmatched uri is
        an HONEST note, never a silent no-op; headless is a no-op."""
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
            from qgis.core import QgsGeometry

            dest_crs = canvas.mapSettings().destinationCrs()
            extent = reproject(layer.extent(), layer.crs(), dest_crs)
            if extent is None:
                self._note(
                    f"Could not put '{layer.name()}' into the canvas CRS "
                    f"({layer.crs().authid()} -> {dest_crs.authid()})",
                    error=True,
                )
                return
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
        """A loaded layer matching ``source_uri``: the exact stamp first, else a substring
        match either way, because two uris for one object can differ in scheme or host while
        sharing the key."""
        try:
            from qgis.core import QgsProject

            layers = list(QgsProject.instance().mapLayers().values())
        except Exception:  # noqa: BLE001 -- headless / no project
            return None
        # Pass 1: exact stamp match.
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

    def _present_card(self, card) -> None:
        """Insert one gate card above the terminal stretch and close out the pending assistant
        entry."""
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
        self._close_pending_for_card()
        self._scroll_to_bottom()

    def _reply(self, label: str, send, *args, **kwargs) -> None:
        """One gate answer out through the bridge, a send failure noted by the name of what
        failed. ``exc`` carries transport state and never the arguments, so a secret-bearing
        reply is safe to route through here."""
        try:
            send(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._note(f"{label} send failed: {exc}", error=True)

    def _begin_turn(self) -> None:
        """Pre-send bookkeeping: a fresh turn starts a fresh "Step N" count and a fresh
        streaming entry. A WS drop can strand an entry that never saw turn-complete, so the
        prior one is final-rendered before the new one is minted."""
        self._tool_picker_turn_step = 0
        if self._pending is not None:
            self._pending.finalize_markdown()
        self._pending = _AssistantEntry(self.messages_layout)
        self._scroll_to_bottom()

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
        self._present_card(card)

    def _on_gate_decision(
        self, warning_id: str, decision: str, revised_args: Optional[dict]
    ) -> None:
        self._reply("confirmation", self.bridge.confirm_payload,
                    warning_id, decision, revised_args)

    # -- code-exec approval card ----------------------------------------------- #

    def _show_code_exec_card(self, payload: dict) -> None:
        """Render the code-exec HARD confirm gate as an inline approval card. The agent does
        not send the code until the decision rides back, so a malformed envelope is noted
        honestly rather than dropped."""
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
        # Track the card by code_exec_id so the following processing-request folds its outcome into it.
        self._code_exec_cards[request.code_exec_id] = card
        self._present_card(card)

    def _on_code_exec_decision(self, code_exec_id: str, decision: str) -> None:
        """Send the code-exec confirmation."""
        self._reply("code-exec confirmation", self.bridge.confirm_payload,
                    code_exec_id, decision, None)

    def _on_layer_request(self, payload: dict) -> None:
        """Open the borrowed provider layer in this session and answer it."""
        response = run_layer_request(
            payload, base_url=self._effective_http_base(), iface=self.iface
        )
        self._reply("layer response", self.bridge.send_layer_response, **response)
        if response.get("error"):
            self._note(
                f"Could not open {payload.get('name') or 'the requested layer'}: "
                f"{response['error']}",
                error=True,
            )
        elif payload.get("mode") == "open":
            self._ensure_pending().add_note(
                f"Added {payload.get('name') or 'a layer'} to the map from the "
                f"{payload.get('provider')} provider"
            )
            self._scroll_to_bottom()

    def _on_processing_request(self, payload: dict) -> None:
        """Run the request in this session and answer it; a code request also folds its outcome
        into the card that approved it."""
        response = run_processing_request(payload, iface=self.iface)
        self._reply("processing response", self.bridge.send_processing_response,
                    **response)
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
            # The C++ widget died (case switch raced the result).
            self._code_exec_cards.pop(code_exec_id, None)

    def _on_secrets_list(self, payload: dict) -> None:
        """Store the ``secrets-list`` roster as the durable state the settings dialog reads.
        Raw keys NEVER ride this envelope, and a one-line count note lands in chat so the
        refresh is visible."""
        self._secrets = gate.parse_secrets_list(payload)
        active = [s for s in self._secrets if getattr(s, "is_active", True)]
        if active:
            self._ensure_pending().add_note(
                f"Saved API keys: {len(active)} "
                + "(" + ", ".join(s.display for s in active[:6])
                + (", ..." if len(active) > 6 else "") + ")"
            )
            self._scroll_to_bottom()

    def _show_spatial_input_card(self, payload: dict) -> None:
        """Render the spatial-input gate as an inline pick card, wired to the SAME canvas
        machinery the probe and AOI tools use."""
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
            # point-1, point-2, ... so unedited names still tell picks apart.
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
        self._present_card(card)

    def _on_spatial_input_decision(self, wire: dict) -> None:
        """Send the spatial-input reply: coordinates, features, or a cancel."""
        self._reply("spatial input", self.bridge.send_spatial_input,
                    wire["request_id"],
                    geometry_type=wire.get("geometry_type"),
                    coordinates=wire.get("coordinates"),
                    features=wire.get("features"),
                    name=wire.get("name"),
                    cancelled=bool(wire.get("cancelled")))

    def _show_tool_candidates_card(self, payload: dict) -> None:
        """Render the tool-candidates picker as an inline card."""
        request = gate.parse_tool_candidates(payload)
        if request is None:
            self._note(
                "Received a malformed tool-candidates request (no request_id)"
                " -- cannot answer it; the agent will proceed with its own "
                "pick after its timeout.",
                error=True,
            )
            return
        # Increment BEFORE constructing the card so a wave of N pickers reads "Step 1", "Step 2", ...
        self._tool_picker_turn_step += 1
        card = ToolCandidatesCard(
            request, self._on_tool_choice, step_index=self._tool_picker_turn_step
        )
        self._present_card(card)
        self._open_tool_pickers.append(card)

    def _on_tool_choice(
        self, request_id: str, tool_name: Optional[str], free_text: Optional[str]
    ) -> None:
        """Send the picker reply:"""
        self._reply("tool choice", self.bridge.send_tool_choice, request_id,
                    tool_name=tool_name, free_text=free_text)

    def _supersede_open_tool_pickers(self) -> None:
        """Fold every still-open picker to its "agent proceeded" chip, the turn having moved
        on, and drop terminal cards from the list. An ANSWERED card is only collected here,
        never re-folded."""
        if not self._open_tool_pickers:
            return
        for card in self._open_tool_pickers:
            try:
                card.mark_superseded()
            except RuntimeError:
                # The C++ widget died; the ref is dropped below.
                pass
        self._open_tool_pickers = [
            c for c in self._open_tool_pickers if not self._picker_terminal(c)
        ]

    @staticmethod
    def _picker_terminal(card: ToolCandidatesCard) -> bool:
        """True when the card reached ANY terminal state (answered or superseded) OR its widget
        already died -- either way it needs no further tracking. Never raises."""
        try:
            return card.answered
        except RuntimeError:
            return True

    def _send_run_invocation(self, text, run_inv) -> None:
        """Handle a parsed ``!run`` invocation."""
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
        self._begin_turn()
        try:
            self.bridge.send_dev_tool_invoke(
                run_inv.name, run_inv.args, raw_text=text
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"!run send failed: {exc}", error=True)

    def _send(self) -> None:
        # Read the full document (toPlainText); the composer is multi-line.
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        if not (self._case_id and self.bridge.running):
            self.status_label.setText("Not connected -- open Settings to connect")
            return
        self.input_edit.remember(text)
        # !run is PARSE-FIRST: a ``!run`` prefix routes to the server as ``dev-tool-invoke``; a message that merely mentions it flows to chat.
        run_inv = parse_run_invocation(text)
        if run_inv is not None:
            self._send_run_invocation(text, run_inv)
            return
        self.input_edit.clear()
        self._add_user_bubble(text)
        self._begin_turn()
        bbox = self._aoi_for_send()
        # The AOI rides ``aoi_bbox``; the chat text goes out CLEAN, and the transcript note fires only when the user sets or changes the AOI.
        try:
            # show_thinking enables reasoning-channel forwarding for this turn (local mode only).
            # The picked model_id (empty = agent env default) applies live on the next message; provider base_url/key stay agent-process env.
            # The persisted Auto/Ask mode makes the server surface picker cards in ask mode ("auto" is omitted; it is the default).
            self.bridge.send_chat(
                text,
                show_thinking=self.settings.show_thinking,
                model_id=self.settings.model_id,
                aoi_bbox=bbox,
                tool_choice_mode=self.settings.tool_choice_mode,
                # Attach any pending drawn region to THIS turn (one rectangle, one turn).
                drawn_geometry=self._drawn_region,
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.add_note(f"send failed: {exc}", error=True)
        # Clear-on-send: the drawn region rides exactly one message.
        if self._drawn_region is not None:
            self._clear_region_overlay()

    def shutdown(self) -> None:
        # Unhook the layer-tree push actions so a plugin reload never stacks duplicate menu entries.
        for action in self._push_tree_actions:
            try:
                self.iface.removeCustomActionForLayerType(action)
            except Exception:  # noqa: BLE001
                pass
        self._push_tree_actions = []
        # The charts window is a sibling dock this dock owns; tear it down so a reload leaves no orphan.
        if self._charts_window is not None:
            try:
                self.iface.removeDockWidget(self._charts_window)
            except Exception:  # noqa: BLE001 -- headless / never docked
                pass
            self._charts_window.deleteLater()
            self._charts_window = None
        if self._library_window is not None:
            try:
                self.iface.removeDockWidget(self._library_window)
            except Exception:  # noqa: BLE001 -- headless / never docked
                pass
            self._library_window.deleteLater()
            self._library_window = None
        self.bridge.stop()
        # Dock close / plugin unload ends the session: remove its staging dir.
        self.materializer.cleanup_session()
