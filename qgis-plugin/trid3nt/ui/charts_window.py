"""The Charts window -- a TUFLOW-Viewer-style bottom dock for the session's
charts (NATE charts-window directive 2026-08-04).

The charts used to render in a small collapsible "Charts (N)" panel pinned
under the chat message list (``charts.ChartsPanel``, now deleted). NATE's
directive: charts get their OWN window that "looks and operates similar to
tuflow viewer, where it has interactive maps and displays at the bottom of
the app window horizontally". This module is that window:

* ``ChartsWindow`` -- a ``QDockWidget`` the dock docks to
  ``Qt.BottomDockWidgetArea`` of the QGIS main window (the TUFLOW Viewer
  position). Horizontal layout: a thin chart-list strip (left) for switching
  among the session's charts, the chart canvas centre-stage, a matplotlib
  navigation toolbar (pan / x-zoom / home) above it, and a status row below
  (hover readout + click-inspect readout + a "Locate on map" affordance).
  Floating / re-docking is native ``QDockWidget``.

* The pure Vega-Lite -> matplotlib renderer stays in ``charts.render_spec``
  (byte-identical -- it only changed homes); this module imports it. The
  window adds the INTERACTIVITY the inline panel never had:

  (a) HOVER readout -- ``motion_notify_event`` reports the cursor's data
      coords in a status row.
  (b) CLICK-TO-INSPECT -- ``button_press_event`` finds the nearest plotted
      vertex (line series + scatter collections), highlights it, and shows
      its value + series label.
  (c) X-ZOOM / PAN -- the matplotlib ``NavigationToolbar`` (pan, rubber-band
      zoom, home) is embedded; a plain scroll-wheel also zooms the x-axis
      about the cursor.
  (d) MAP LINKAGE -- a chart that carries a ``source_layer_uri`` gets a
      "Locate on map" button; clicking it calls back into the dock to pan +
      flash the QGIS canvas to that layer's extent (the honest reachable
      half of TUFLOW's map<->plot linking; per-feature click->plot linking
      needs per-feature series data the ``chart-emission`` payload does not
      carry today -- see ADR 0119).

Durability: the window's chart list rebuilds from the persisted
``SessionChartRecord`` replay on every case open (``set_charts``); a case
switch clears it (``clear``) -- charts are per-Case state (the per-case
durability norm).

matplotlib import is GUARDED in ``charts``: when absent the chart canvas
degrades to ``MissingMatplotlibPanel`` -- what's missing, why, the exact
per-OS pip command (copy button), and an "Attempt install" button that runs
it via ``QProcess`` with streamed output -- never a crash and never a silent
auto-install.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from qgis.PyQt.QtCore import QProcess, Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import charts

# Navigation toolbar for (c) -- guarded exactly like the canvas class in
# ``charts``: absent matplotlib means no toolbar, and the window falls back to
# the text list anyway.
_NAV_TOOLBAR = None
if charts.matplotlib_available():
    try:
        from matplotlib.backends.backend_qtagg import (
            NavigationToolbar2QT as _NAV_TOOLBAR,
        )
    except Exception:  # noqa: BLE001 -- older matplotlib fallback
        try:
            from matplotlib.backends.backend_qt5agg import (
                NavigationToolbar2QT as _NAV_TOOLBAR,
            )
        except Exception:  # noqa: BLE001 -- no toolbar, canvas still renders
            _NAV_TOOLBAR = None


_CANVAS_MIN_HEIGHT = 220  # px -- the bottom dock is short + wide (TUFLOW-esque)
_LIST_WIDTH = 180  # px -- the thin chart-switcher strip


class MissingMatplotlibPanel(QWidget):
    """The guided fix shown in place of the chart canvas when matplotlib is
    unavailable in this QGIS python (QGIS 4's macOS/Windows bundles dropped
    it; QGIS 3 shipped it). Explains what's missing and why, offers the
    exact per-OS pip command (copy button) derived from ``charts
    .install_command_str`` -- never hardcoded -- and an "Attempt install"
    button that runs the bundled interpreter against the shared
    ``install_dependencies.py`` script via ``QProcess`` (not raw pip -- one
    source of truth with the standalone script, see ``charts
    .install_command_argv``), streaming stdout+stderr live into this panel.
    Never installs without the click. On success, prompts to reopen the
    chart dock (and offers an in-place reload via ``on_reload``, which the
    window wires to a cache-bust + re-render so the user does not have to
    close/reopen anything). On failure, the streamed output stays on
    screen -- an honest failure, no silent retry.
    """

    def __init__(
        self,
        on_reload: Optional[Callable[[], bool]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._on_reload = on_reload
        self._process: Optional[QProcess] = None
        self._command = charts.install_command_str()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        why = QLabel(
            "Charts need matplotlib, which this QGIS's Python does not have.\n"
            "QGIS 4 no longer bundles matplotlib in its Python environment "
            "(QGIS 3 did) -- it is a one-time pip install into QGIS's own "
            "interpreter, not a TRID3NT bug.\n\n"
            f"Reason: {charts.matplotlib_error()}"
        )
        why.setWordWrap(True)
        root.addWidget(why)

        cmd_row = QHBoxLayout()
        self._cmd_label = QLabel(self._command)
        self._cmd_label.setWordWrap(True)
        self._cmd_label.setStyleSheet(
            "font-family: monospace; font-size: 8pt; padding: 4px; "
            "background: rgba(127,127,127,0.15); border-radius: 3px;"
        )
        self._cmd_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        cmd_row.addWidget(self._cmd_label, 1)
        copy_btn = QToolButton()
        copy_btn.setText("Copy")
        copy_btn.setToolTip("Copy the install command to the clipboard")
        copy_btn.clicked.connect(self._on_copy)
        cmd_row.addWidget(copy_btn)
        root.addLayout(cmd_row)

        action_row = QHBoxLayout()
        self.install_btn = QPushButton("Attempt install")
        self.install_btn.setToolTip(
            "Run install_dependencies.py via QGIS's own Python -- streams "
            "output below; never runs without this click"
        )
        self.install_btn.clicked.connect(self._on_attempt_install)
        action_row.addWidget(self.install_btn)
        self.reload_btn = QPushButton("Reopen chart")
        self.reload_btn.setToolTip(
            "Re-check for matplotlib and re-render this chart"
        )
        self.reload_btn.setEnabled(False)
        self.reload_btn.clicked.connect(self._on_reload_clicked)
        action_row.addWidget(self.reload_btn)
        action_row.addStretch(1)
        root.addLayout(action_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setStyleSheet("font-family: monospace; font-size: 8pt;")
        self.output.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.output.setVisible(False)
        root.addWidget(self.output, 1)

    # -- copy ------------------------------------------------------------- #

    def _on_copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._command)
        self.status_label.setText("Command copied to clipboard.")

    # -- attempt install (QProcess, streamed) ------------------------------ #

    def _on_attempt_install(self) -> None:
        if self._process is not None:
            return  # already running -- the button is disabled meanwhile
        argv = charts.install_command_argv()
        self.install_btn.setEnabled(False)
        self.reload_btn.setEnabled(False)
        self.status_label.setText(f"Running: {self._command}")
        self.output.setVisible(True)
        self.output.clear()

        proc = QProcess(self)
        proc.setProgram(argv[0])
        proc.setArguments(argv[1:])
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error_occurred)
        self._process = proc
        proc.start()

    def _on_stdout(self) -> None:
        if self._process is not None:
            self.output.appendPlainText(
                bytes(self._process.readAllStandardOutput()).decode(
                    "utf-8", "replace"
                )
            )

    def _on_stderr(self) -> None:
        if self._process is not None:
            self.output.appendPlainText(
                bytes(self._process.readAllStandardError()).decode(
                    "utf-8", "replace"
                )
            )

    def _on_error_occurred(self, error) -> None:
        # QProcess failed to even start (bad path, permissions, ...) --
        # honest surfacing, same as a nonzero exit.
        self.output.appendPlainText(f"\nfailed to start process: {error}")

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        self._process = None
        self.install_btn.setEnabled(True)
        if exit_code == 0:
            self.status_label.setText(
                "Install finished. Click 'Reopen chart' to load it now "
                "(or close and reopen the TRID3NT Charts window)."
            )
            self.reload_btn.setEnabled(True)
        else:
            self.status_label.setText(
                f"Install failed (exit code {exit_code}) -- see output above."
            )

    def _on_reload_clicked(self) -> None:
        if self._on_reload is not None:
            self._on_reload()


class ChartsWindow(QDockWidget):
    """The session's charts, in a bottom-docked TUFLOW-Viewer-style window.

    Public API mirrors the deleted ``ChartsPanel`` so the dock wiring is a
    drop-in swap: ``set_charts`` (case-open replay), ``add_chart`` (live
    ``chart-emission`` frame, de-duped on ``chart_id``), ``clear`` (case
    switch), ``count`` / ``current_chart_id``. ``last_render_summary`` carries
    the current chart's ``render_spec`` output for the headless harness.

    ``locate_callback`` is the dock's ``_locate_layer_on_map`` -- invoked with
    a chart's ``source_layer_uri`` when the user clicks "Locate on map". None
    (headless / no callback) simply disables the button.
    """

    def __init__(
        self,
        locate_callback: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__("TRID3NT Charts", parent)
        self.setObjectName("Trid3ntChartsWindow")
        self._charts: List[dict] = []
        self._index = 0
        self._locate_callback = locate_callback
        #: Render summary of the currently shown chart (``render_spec``
        #: output, or ``{"fallback": True}`` without matplotlib) -- the
        #: offscreen harness asserts series/rule/scale counts on it.
        self.last_render_summary: Optional[Dict[str, Any]] = None
        #: The matplotlib axes of the current chart (for hover / click-inspect
        #: geometry); None without matplotlib or before the first chart.
        self._ax = None
        self._figure = None
        self._canvas = None
        self._highlight = None  # the click-inspect highlight artist
        self._motion_cid = None
        self._press_cid = None
        self._scroll_cid = None

        self._build_ui()
        self.setVisible(False)

    # -- UI scaffold ---------------------------------------------------------- #

    def _build_ui(self) -> None:
        body = QWidget()
        root = QHBoxLayout(body)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # Left: the thin chart-switcher strip (the session's charts by title).
        self.chart_list = QListWidget()
        self.chart_list.setFixedWidth(_LIST_WIDTH)
        self.chart_list.currentRowChanged.connect(self._on_list_row)
        root.addWidget(self.chart_list)

        # Right: nav toolbar (top) + canvas (centre) + status + caption + paging.
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(2)

        self._toolbar_host = QHBoxLayout()
        self._toolbar_host.setContentsMargins(0, 0, 0, 0)
        right.addLayout(self._toolbar_host)
        self._toolbar = None  # rebuilt per canvas (bound to that canvas)

        self._canvas_host = QVBoxLayout()
        self._canvas_host.setContentsMargins(0, 0, 0, 0)
        right.addLayout(self._canvas_host, 1)

        # Status row: hover readout (left) + Locate-on-map (right).
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        self.hover_label = QLabel("")
        self.hover_label.setStyleSheet("font-size: 8pt; color: #888;")
        status_row.addWidget(self.hover_label, 1)
        self.locate_btn = QToolButton()
        self.locate_btn.setText("Locate on map")
        self.locate_btn.setToolTip(
            "Pan + flash the QGIS canvas to the layer this chart was "
            "computed from (charts that carry a source layer)"
        )
        self.locate_btn.clicked.connect(self._on_locate)
        self.locate_btn.setEnabled(False)
        status_row.addWidget(self.locate_btn)
        right.addLayout(status_row)

        # Click-inspect readout (nearest-vertex value + series label).
        self.inspect_label = QLabel("")
        self.inspect_label.setStyleSheet("font-size: 8pt;")
        self.inspect_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        right.addWidget(self.inspect_label)

        # Caption (chart's one-line interpretation).
        self.caption_label = QLabel("")
        self.caption_label.setWordWrap(True)
        self.caption_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.caption_label.setVisible(False)
        right.addWidget(self.caption_label)

        # Paging row (prev / N-of-M / next) -- redundant with the list but kept
        # per the directive; steps the same current index.
        paging = QHBoxLayout()
        paging.setContentsMargins(0, 0, 0, 0)
        self.prev_btn = QToolButton()
        self.prev_btn.setText("<")
        self.prev_btn.setAutoRaise(True)
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        paging.addWidget(self.prev_btn)
        self.pos_label = QLabel("")
        paging.addWidget(self.pos_label)
        self.next_btn = QToolButton()
        self.next_btn.setText(">")
        self.next_btn.setAutoRaise(True)
        self.next_btn.clicked.connect(lambda: self._step(1))
        paging.addWidget(self.next_btn)
        paging.addStretch(1)
        right.addLayout(paging)

        root.addLayout(right, 1)
        self.setWidget(body)

    # -- public state --------------------------------------------------------- #

    @property
    def count(self) -> int:
        return len(self._charts)

    def current_chart_id(self) -> Optional[str]:
        if 0 <= self._index < len(self._charts):
            return self._charts[self._index].get("chart_id")
        return None

    # -- dock-facing API (drop-in for the old ChartsPanel) -------------------- #

    def set_charts(self, payloads: list) -> int:
        """Replace-all for the case-open replay (``session_state.charts``,
        persisted oldest-first). Shows the NEWEST chart. Returns the count of
        usable charts (the per-case durability rebuild)."""
        self._charts = []
        seen = set()
        for raw in payloads or []:
            chart = charts.parse_chart_payload(raw)
            if chart is None or chart["chart_id"] in seen:
                continue
            seen.add(chart["chart_id"])
            self._charts.append(chart)
        self._index = max(0, len(self._charts) - 1)
        self._refresh()
        return len(self._charts)

    def add_chart(self, payload: Any) -> bool:
        """One live ``chart-emission`` frame. De-dupes on chart_id (a re-emit
        re-shows the existing entry). Returns True when a NEW chart was
        added."""
        chart = charts.parse_chart_payload(payload)
        if chart is None:
            return False
        for i, existing in enumerate(self._charts):
            if existing.get("chart_id") == chart["chart_id"]:
                self._index = i
                self._refresh()
                return False
        self._charts.append(chart)
        self._index = len(self._charts) - 1
        self._refresh()
        return True

    def clear(self) -> None:
        """Case switch: charts are per-Case state (per-case durability norm)."""
        self._charts = []
        self._index = 0
        self._refresh()

    # -- internals ------------------------------------------------------------ #

    def _on_list_row(self, row: int) -> None:
        if 0 <= row < len(self._charts) and row != self._index:
            self._index = row
            self._refresh()

    def _step(self, delta: int) -> None:
        if not self._charts:
            return
        self._index = max(0, min(len(self._charts) - 1, self._index + delta))
        self._refresh()

    def _refresh(self) -> None:
        n = len(self._charts)
        # Rebuild the switcher strip (block signals -- we drive the index).
        self.chart_list.blockSignals(True)
        self.chart_list.clear()
        for chart in self._charts:
            self.chart_list.addItem(
                chart.get("title") or chart.get("chart_id") or "chart"
            )
        if 0 <= self._index < n:
            self.chart_list.setCurrentRow(self._index)
        self.chart_list.blockSignals(False)

        paging = n > 1
        self.prev_btn.setVisible(paging)
        self.next_btn.setVisible(paging)
        self.pos_label.setVisible(paging)
        if paging:
            self.pos_label.setText(f"{self._index + 1}/{n}")
            self.prev_btn.setEnabled(self._index > 0)
            self.next_btn.setEnabled(self._index < n - 1)

        self._teardown_canvas()
        self.last_render_summary = None
        self.hover_label.setText("")
        self.inspect_label.setText("")
        if n == 0:
            self.caption_label.setVisible(False)
            self.locate_btn.setEnabled(False)
            return

        chart = self._charts[self._index]
        self._build_canvas(chart)
        caption = chart.get("caption")
        self.caption_label.setText(caption if isinstance(caption, str) else "")
        self.caption_label.setVisible(bool(caption))
        # (d) Locate-on-map only when the chart carries a source layer AND the
        # dock wired a callback (headless has neither).
        source_uri = chart.get("source_layer_uri")
        self.locate_btn.setEnabled(
            bool(source_uri) and self._locate_callback is not None
        )

    def _teardown_canvas(self) -> None:
        """Drop the current canvas + toolbar and disconnect its mpl event
        callbacks (a fresh chart gets a fresh canvas bound to its own axes)."""
        for cid_attr in ("_motion_cid", "_press_cid", "_scroll_cid"):
            cid = getattr(self, cid_attr)
            if cid is not None and self._canvas is not None:
                try:
                    self._canvas.mpl_disconnect(cid)
                except Exception:  # noqa: BLE001
                    pass
            setattr(self, cid_attr, None)
        if self._toolbar is not None:
            self._toolbar_host.removeWidget(self._toolbar)
            self._toolbar.deleteLater()
            self._toolbar = None
        while self._canvas_host.count():
            item = self._canvas_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._canvas = None
        self._figure = None
        self._ax = None
        self._highlight = None

    def _build_canvas(self, chart: dict) -> None:
        """The rendered chart + its interactivity, or the guided
        ``MissingMatplotlibPanel`` fix when matplotlib is unavailable in
        this QGIS python."""
        if not charts.matplotlib_available():
            self.last_render_summary = {"fallback": True}
            panel = MissingMatplotlibPanel(on_reload=self._on_matplotlib_reload)
            self._canvas_host.addWidget(panel)
            return

        figure = charts.Figure(figsize=(6.0, _CANVAS_MIN_HEIGHT / 100.0), dpi=100)
        canvas = charts.FigureCanvasQTAgg(figure)
        canvas.setMinimumHeight(_CANVAS_MIN_HEIGHT)
        spec = chart.get("vega_lite_spec") or {}
        self.last_render_summary = charts.render_spec(figure, spec)
        canvas.draw()
        self._canvas_host.addWidget(canvas)
        self._figure = figure
        self._canvas = canvas
        self._ax = figure.axes[0] if figure.axes else None

        # (c) navigation toolbar (pan / rubber-band zoom / home).
        if _NAV_TOOLBAR is not None:
            self._toolbar = _NAV_TOOLBAR(canvas, self)
            self._toolbar_host.addWidget(self._toolbar)

        # (a)/(b)/(c) mpl event callbacks bound to THIS canvas.
        self._motion_cid = canvas.mpl_connect(
            "motion_notify_event", self._on_motion
        )
        self._press_cid = canvas.mpl_connect(
            "button_press_event", self._on_press
        )
        self._scroll_cid = canvas.mpl_connect(
            "scroll_event", self._on_scroll
        )

    def _on_matplotlib_reload(self) -> bool:
        """MissingMatplotlibPanel's "Reopen chart" -- bust the cached import
        failure, retry, and re-render the current chart in place when it
        now succeeds (post a successful "Attempt install"). Returns whether
        matplotlib is now available."""
        global _NAV_TOOLBAR
        available = charts.recheck_matplotlib()
        if available and _NAV_TOOLBAR is None:
            try:
                from matplotlib.backends.backend_qtagg import (
                    NavigationToolbar2QT as _toolbar_cls,
                )
            except Exception:  # noqa: BLE001
                try:
                    from matplotlib.backends.backend_qt5agg import (
                        NavigationToolbar2QT as _toolbar_cls,
                    )
                except Exception:  # noqa: BLE001
                    _toolbar_cls = None
            _NAV_TOOLBAR = _toolbar_cls
        if available:
            self._refresh()
        return available

    # -- interactivity: hover (a) -------------------------------------------- #

    def _on_motion(self, event) -> None:
        if event.inaxes is None or event.xdata is None or event.ydata is None:
            self.hover_label.setText("")
            return
        self.hover_label.setText(
            f"x = {event.xdata:.4g}    y = {event.ydata:.4g}"
        )

    # -- interactivity: click-to-inspect (b) --------------------------------- #

    def nearest_vertex(self, x_pixel: float, y_pixel: float):
        """Nearest plotted vertex to a DISPLAY-space point, across every line
        series and scatter collection on the current axes. Returns
        ``(x_data, y_data, label)`` or None. Pure geometry (display-space
        distance so log axes + differing x/y scales compare fairly) so the
        harness can assert it without synthesizing a mouse event."""
        if self._ax is None:
            return None
        best = None
        best_d2 = float("inf")

        def _consider(xd, yd, label):
            nonlocal best, best_d2
            try:
                px, py = self._ax.transData.transform((xd, yd))
            except Exception:  # noqa: BLE001
                return
            d2 = (px - x_pixel) ** 2 + (py - y_pixel) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = (float(xd), float(yd), label)

        for line in self._ax.get_lines():
            label = line.get_label()
            if isinstance(label, str) and label.startswith("_"):
                label = ""
            xs = line.get_xdata()
            ys = line.get_ydata()
            for xd, yd in zip(xs, ys):
                _consider(xd, yd, label)
        for coll in self._ax.collections:
            try:
                offsets = coll.get_offsets()
            except Exception:  # noqa: BLE001
                continue
            for pt in offsets:
                if len(pt) >= 2:
                    _consider(pt[0], pt[1], "")
        return best

    def _on_press(self, event) -> None:
        if event.inaxes is None or self._ax is None:
            return
        hit = self.nearest_vertex(event.x, event.y)
        if hit is None:
            return
        xd, yd, label = hit
        prefix = f"{label}: " if label else ""
        self.inspect_label.setText(f"{prefix}x = {xd:.6g}, y = {yd:.6g}")
        # Highlight the picked vertex (drop the previous marker first).
        if self._highlight is not None:
            try:
                self._highlight.remove()
            except Exception:  # noqa: BLE001
                pass
            self._highlight = None
        try:
            self._highlight = self._ax.scatter(
                [xd], [yd], s=90, facecolors="none",
                edgecolors="#c1121f", linewidths=1.6, zorder=10,
            )
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001 -- highlight is best-effort chrome
            pass

    # -- interactivity: wheel x-zoom (c) ------------------------------------- #

    def _on_scroll(self, event) -> None:
        if event.inaxes is None or self._ax is None or event.xdata is None:
            return
        # Zoom the x-axis about the cursor: wheel up = in, down = out.
        scale = 0.8 if event.button == "up" else 1.25
        x0, x1 = self._ax.get_xlim()
        left = event.xdata - (event.xdata - x0) * scale
        right = event.xdata + (x1 - event.xdata) * scale
        try:
            self._ax.set_xlim(left, right)
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001
            pass

    # -- interactivity: map linkage (d) -------------------------------------- #

    def _on_locate(self) -> None:
        chart = (
            self._charts[self._index]
            if 0 <= self._index < len(self._charts)
            else None
        )
        if chart is None or self._locate_callback is None:
            return
        source_uri = chart.get("source_layer_uri")
        if isinstance(source_uri, str) and source_uri:
            self._locate_callback(source_uri)
