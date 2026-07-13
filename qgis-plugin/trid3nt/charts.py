"""Charts surface for the TRID3NT dock (live-feedback 2026-07-13, OpenQuake
result parity).

The web UI renders the agent's ``chart-emission`` payloads (Vega-Lite v5
specs -- contracts ``chart_contracts.ChartEmissionPayload``) as inline chart
cards; the QGIS dock surfaced layers only, so an OpenQuake PSHA case lost its
"Seismic hazard curve - PGA" chart entirely. This module closes that gap:

* ``parse_chart_payload`` / spec helpers -- defensive, pure-python handling
  of the wire payload (chart_id + title + caption + vega_lite_spec).
* ``render_spec`` -- a deliberately SMALL Vega-Lite interpreter that draws
  the subset our agent actually emits (see ``chart_tools.py``: line+point,
  dashed rule reference lines, bar, rect/heatmap) onto a matplotlib Figure.
  It is NOT a general Vega renderer; unknown marks are skipped and counted,
  never crashed on -- a malformed persisted spec must not break a case open.
* ``ChartsPanel`` -- the dock widget: a collapsible "Charts (N)" panel pinned
  under the message list (the probe-panel pattern, BUG 3b precedent), with
  prev/next paging across the case's charts. Charts NEVER land in the chat
  message list (NATE's clutter rule).

Rendering choice (researched 2026-07-13): matplotlib ``FigureCanvasQTAgg``
embedded in the dock. Debian QGIS 3.40 ships matplotlib (3.10) in the same
system python as PyQt5, the QtAgg backend binds to the already-imported
qgis.PyQt binding, and it gives log-log axes / legends / dashed rules for
free -- the hazard curve is log-log, which pure-QPainter code would have to
hand-roll. GEM's IRMT plugin was rejected (not installed, its viewer is
coupled to OQ-engine NRML outputs, not our Vega payloads); a server-side PNG
render was rejected (server change + restart + flood smoke for zero offline
benefit). matplotlib import is GUARDED: when absent the panel degrades to an
honest text card (title + caption + why), never a crash.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# -- guarded matplotlib import (see module docstring) ------------------------ #
# ``Figure`` + a Qt canvas class, no pyplot (pyplot owns global backend state
# we must not fight QGIS for). backend_qtagg resolves its binding via the
# already-imported qgis.PyQt (PyQt5); backend_qt5agg is the pre-3.5 fallback.
_MATPLOTLIB_ERROR: Optional[str] = None
try:  # noqa: SIM105
    from matplotlib.figure import Figure

    try:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    except ImportError:  # older matplotlib
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
except Exception as _exc:  # noqa: BLE001 -- absence is a supported state
    Figure = None  # type: ignore[assignment]
    FigureCanvasQTAgg = None  # type: ignore[assignment]
    _MATPLOTLIB_ERROR = f"{type(_exc).__name__}: {_exc}"


def matplotlib_available() -> bool:
    return _MATPLOTLIB_ERROR is None


# Default series colors (matplotlib tab10 order) for color-field grouping.
_SERIES_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]

_CANVAS_HEIGHT = 260  # px -- one chart card, dock-width


# --------------------------------------------------------------------------- #
# Pure payload/spec helpers (no matplotlib needed)
# --------------------------------------------------------------------------- #


def parse_chart_payload(payload: Any) -> Optional[dict]:
    """A wire/persisted ``ChartEmissionPayload`` dict -> the same dict, or
    None when it is unusable (no chart_id / no dict spec). Defensive: the
    replayed ``session_state.charts`` rows are persisted data -- a bad row
    is skipped, never raised on."""
    if not isinstance(payload, dict):
        return None
    chart_id = payload.get("chart_id")
    spec = payload.get("vega_lite_spec")
    if not isinstance(chart_id, str) or not chart_id:
        return None
    if not isinstance(spec, dict) or not spec:
        return None
    return payload


def spec_title(spec: dict) -> str:
    """Vega-Lite ``title`` is a string or a ``{"text": ...}`` object."""
    title = spec.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    return title if isinstance(title, str) else ""


def spec_views(spec: dict) -> List[dict]:
    """Normalize a layered spec (``{"layer": [...]}`` -- the hazard curve's
    line+rule shape) and a single-view spec into a flat view list."""
    layer = spec.get("layer")
    if isinstance(layer, list):
        return [v for v in layer if isinstance(v, dict)]
    return [spec]


def view_rows(view: dict, spec: dict) -> List[dict]:
    """The inline data rows for one view: view-level ``data.values`` first,
    falling back to the top-level spec's (Vega-Lite layer inheritance)."""
    for carrier in (view, spec):
        data = carrier.get("data")
        if isinstance(data, dict) and isinstance(data.get("values"), list):
            return [r for r in data["values"] if isinstance(r, dict)]
    return []


def _mark_type(view: dict) -> str:
    mark = view.get("mark")
    if isinstance(mark, str):
        return mark
    if isinstance(mark, dict):
        return str(mark.get("type") or "")
    return ""


def _mark_props(view: dict) -> dict:
    mark = view.get("mark")
    return mark if isinstance(mark, dict) else {}


def _channel(view: dict, name: str) -> dict:
    enc = view.get("encoding")
    if isinstance(enc, dict) and isinstance(enc.get(name), dict):
        return enc[name]
    return {}


def _is_log(channel: dict) -> bool:
    scale = channel.get("scale")
    return isinstance(scale, dict) and scale.get("type") == "log"


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# --------------------------------------------------------------------------- #
# The mini Vega-Lite -> matplotlib renderer
# --------------------------------------------------------------------------- #


def render_spec(figure, spec: dict) -> Dict[str, Any]:
    """Draw ``spec`` (the emitted subset -- module docstring) onto ``figure``.

    Returns a summary dict the harness asserts on: ``views`` / ``lines`` /
    ``series`` / ``rules`` / ``bars`` / ``points`` (line vertices drawn) /
    ``skipped`` counts plus ``x_log`` / ``y_log`` flags and the collected
    ``legend_labels``. Never raises on spec content -- an unusable view is
    counted in ``skipped``.
    """
    summary: Dict[str, Any] = {
        "views": 0, "lines": 0, "series": 0, "rules": 0, "bars": 0,
        "points": 0, "skipped": 0, "x_log": False, "y_log": False,
        "legend_labels": [],
    }
    ax = figure.add_subplot(111)
    ax.tick_params(labelsize=7)
    ax.grid(True, which="both", alpha=0.25, linewidth=0.5)

    for view in spec_views(spec):
        summary["views"] += 1
        mark = _mark_type(view)
        props = _mark_props(view)
        rows = view_rows(view, spec)
        xch, ych = _channel(view, "x"), _channel(view, "y")
        xf, yf = xch.get("field"), ych.get("field")
        try:
            if mark == "line" and rows and xf and yf:
                summary["lines"] += 1
                summary["series"] += _draw_line(ax, rows, xf, yf, view, props, summary)
            elif mark == "rule" and rows:
                _draw_rules(ax, rows, xf, yf, props, summary)
            elif mark == "bar" and rows and xf and yf:
                summary["bars"] += _draw_bars(ax, rows, xf, yf, view)
            elif mark in ("rect", "point", "circle", "square") and rows and xf and yf:
                # rect (the seawater-intrusion heatmap) degrades to a colored
                # scatter -- honest approximation, cell geometry is not
                # reconstructed. point/circle/square are literal scatters.
                _draw_scatter(ax, rows, xf, yf, view)
            else:
                summary["skipped"] += 1
                continue
        except Exception:  # noqa: BLE001 -- one bad view must not kill the card
            summary["skipped"] += 1
            continue
        # Axes chrome from the first view that carries the channel.
        if _is_log(xch) and not summary["x_log"]:
            ax.set_xscale("log")
            summary["x_log"] = True
        if _is_log(ych) and not summary["y_log"]:
            ax.set_yscale("log")
            summary["y_log"] = True
        if not ax.get_xlabel() and (xch.get("title") or xf):
            ax.set_xlabel(str(xch.get("title") or xf), fontsize=8)
        if not ax.get_ylabel() and (ych.get("title") or yf):
            ax.set_ylabel(str(ych.get("title") or yf), fontsize=8)

    title = spec_title(spec)
    if title:
        ax.set_title(title, fontsize=9)
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        summary["legend_labels"] = list(labels)
        ax.legend(fontsize=7, framealpha=0.6)
    try:
        figure.tight_layout()
    except Exception:  # noqa: BLE001 -- tight_layout can fail on odd extents
        pass
    return summary


def _draw_line(ax, rows, xf, yf, view, props, summary) -> int:
    """Line mark, one plotted series per ``encoding.color.field`` group
    (or one unlabeled series without a color field). Returns series count.

    A non-numeric x (the time-series chart uses ordinal timestamp strings)
    falls back to category positions 0..n-1 with thinned tick labels --
    the same left-to-right reading, no date parsing to get wrong.
    """
    color_field = _channel(view, "color").get("field")
    numeric_x = all(
        _as_float(row.get(xf)) is not None for row in rows if xf in row
    )
    categories: List[str] = []

    def _x_pos(row) -> Optional[float]:
        if numeric_x:
            return _as_float(row.get(xf))
        label = str(row.get(xf))
        if label not in categories:
            categories.append(label)
        return float(categories.index(label))

    groups: Dict[Optional[str], List[tuple]] = {}
    for row in rows:
        x, y = _x_pos(row), _as_float(row.get(yf))
        if x is None or y is None:
            continue
        key = str(row.get(color_field)) if color_field else None
        groups.setdefault(key, []).append((x, y))
    marker = "o" if props.get("point") else None
    n = 0
    for i, (key, pts) in enumerate(groups.items()):
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(
            xs, ys,
            marker=marker, markersize=3, linewidth=1.4,
            color=_SERIES_COLORS[i % len(_SERIES_COLORS)],
            label=key,
        )
        summary["points"] += len(pts)
        n += 1
    if categories:
        # Thin the categorical ticks to at most 8 so timestamps stay legible.
        step = max(1, len(categories) // 8)
        ticks = list(range(0, len(categories), step))
        ax.set_xticks(ticks)
        ax.set_xticklabels(
            [categories[t] for t in ticks], fontsize=6, rotation=30, ha="right"
        )
    return n


def _draw_rules(ax, rows, xf, yf, props, summary) -> None:
    """Rule mark: a constant reference line per row -- horizontal when the
    y channel carries the field (the hazard curve's dashed 10%-in-50yr
    design level), vertical for an x-channel rule (the intrusion-toe
    marker). ``strokeDash`` -> dashed; row ``label`` -> legend entry."""
    linestyle = "--" if props.get("strokeDash") else "-"
    color = props.get("color") or "#c1121f"
    for row in rows:
        label = row.get("label") if isinstance(row.get("label"), str) else None
        if yf is not None:
            value = _as_float(row.get(yf))
            if value is None:
                continue
            ax.axhline(value, linestyle=linestyle, color=color,
                       linewidth=1.1, label=label)
        elif xf is not None:
            value = _as_float(row.get(xf))
            if value is None:
                continue
            ax.axvline(value, linestyle=linestyle, color=color,
                       linewidth=1.1, label=label)
        else:
            continue
        summary["rules"] += 1


def _draw_bars(ax, rows, xf, yf, view) -> int:
    """Bar mark over a categorical x (histogram bins, damage states, budget
    terms). ``encoding.color.field`` maps categories onto the series
    palette. Returns the bar count."""
    color_field = _channel(view, "color").get("field")
    labels: List[str] = []
    heights: List[float] = []
    colors: List[str] = []
    color_keys: Dict[str, str] = {}
    for row in rows:
        y = _as_float(row.get(yf))
        if y is None:
            continue
        labels.append(str(row.get(xf)))
        heights.append(y)
        if color_field:
            key = str(row.get(color_field))
            if key not in color_keys:
                color_keys[key] = _SERIES_COLORS[len(color_keys) % len(_SERIES_COLORS)]
            colors.append(color_keys[key])
        else:
            colors.append(_SERIES_COLORS[0])
    if not heights:
        return 0
    ax.bar(range(len(heights)), heights, color=colors)
    ax.set_xticks(range(len(labels)))
    rotate = any(len(lbl) > 6 for lbl in labels)
    ax.set_xticklabels(
        labels, fontsize=7,
        rotation=30 if rotate else 0,
        ha="right" if rotate else "center",
    )
    return len(heights)


def _draw_scatter(ax, rows, xf, yf, view) -> None:
    """Scatter for point-like marks and the rect degradation. A numeric
    ``encoding.color.field`` colors by value (viridis); else one color."""
    color_field = _channel(view, "color").get("field")
    xs: List[float] = []
    ys: List[float] = []
    cs: List[float] = []
    for row in rows:
        x, y = _as_float(row.get(xf)), _as_float(row.get(yf))
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
        if color_field:
            c = _as_float(row.get(color_field))
            cs.append(c if c is not None else 0.0)
    if not xs:
        return
    if color_field and cs:
        ax.scatter(xs, ys, c=cs, cmap="viridis", s=12)
    else:
        ax.scatter(xs, ys, color=_SERIES_COLORS[0], s=12)


# --------------------------------------------------------------------------- #
# ChartsPanel -- the dock's pinned, collapsible charts surface
# --------------------------------------------------------------------------- #


class ChartsPanel(QWidget):
    """Collapsible "Charts (N)" panel pinned under the message list.

    Mirrors the probe panel (BUG 3b, live-feedback 2026-07-12): charts show
    HERE, never as chat widgets, so a case with many charts cannot flood the
    conversation. One chart is visible at a time; prev/next page through the
    case's charts (newest shown first on arrival). Hidden until the bound
    case has at least one chart; ``clear()`` on every case switch -- charts
    are per-Case state exactly like probe output.

    The dock owns wiring: ``set_charts`` on the case-open replay
    (``session_state.charts``), ``add_chart`` on a live ``chart-emission``
    frame (de-duped by chart_id -- a tool re-emit repaints, never
    duplicates), ``clear`` from ``_clear_messages``.
    """

    def __init__(self, toggle_style: str = "", block_style: str = "", parent=None):
        super().__init__(parent)
        self._charts: List[dict] = []
        self._index = 0
        self._block_style = block_style
        #: Render summary of the currently shown chart (``render_spec``
        #: output, or ``{"fallback": True}`` without matplotlib) -- the
        #: offscreen harness asserts series/rule/scale counts on it.
        self.last_render_summary: Optional[Dict[str, Any]] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.toggle = QPushButton("Charts (0)")
        self.toggle.setFlat(True)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)  # expanded when a chart first lands
        if toggle_style:
            self.toggle.setStyleSheet(toggle_style)
        self.toggle.clicked.connect(self._toggle_body)
        header.addWidget(self.toggle, 1)
        self.prev_btn = QToolButton()
        self.prev_btn.setText("<")
        self.prev_btn.setAutoRaise(True)
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        header.addWidget(self.prev_btn)
        self.pos_label = QLabel("")
        if toggle_style:
            self.pos_label.setStyleSheet(toggle_style)
        header.addWidget(self.pos_label)
        self.next_btn = QToolButton()
        self.next_btn.setText(">")
        self.next_btn.setAutoRaise(True)
        self.next_btn.clicked.connect(lambda: self._step(1))
        header.addWidget(self.next_btn)
        lay.addLayout(header)

        self.body = QWidget()
        self._body_lay = QVBoxLayout(self.body)
        self._body_lay.setContentsMargins(0, 0, 0, 2)
        self._body_lay.setSpacing(2)
        self._card: Optional[QWidget] = None  # the canvas / fallback label
        self.caption_label = QLabel("")
        self.caption_label.setWordWrap(True)
        self.caption_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if block_style:
            self.caption_label.setStyleSheet(block_style)
        self.caption_label.setVisible(False)
        self._body_lay.addWidget(self.caption_label)
        lay.addWidget(self.body)

        self.setVisible(False)

    # -- public state ------------------------------------------------------- #

    @property
    def count(self) -> int:
        return len(self._charts)

    def current_chart_id(self) -> Optional[str]:
        if 0 <= self._index < len(self._charts):
            return self._charts[self._index].get("chart_id")
        return None

    # -- dock-facing API ------------------------------------------------------ #

    def set_charts(self, payloads: list) -> int:
        """Replace-all for the case-open replay (``session_state.charts``,
        persisted oldest-first). Shows the NEWEST chart. Returns the count
        of usable charts."""
        self._charts = []
        seen = set()
        for raw in payloads or []:
            chart = parse_chart_payload(raw)
            if chart is None or chart["chart_id"] in seen:
                continue
            seen.add(chart["chart_id"])
            self._charts.append(chart)
        self._index = max(0, len(self._charts) - 1)
        self._refresh()
        return len(self._charts)

    def add_chart(self, payload: Any) -> bool:
        """One live ``chart-emission`` frame. De-dupes on chart_id (a
        re-emit re-shows the existing entry). Returns True when a NEW chart
        was added."""
        chart = parse_chart_payload(payload)
        if chart is None:
            return False
        for i, existing in enumerate(self._charts):
            if existing.get("chart_id") == chart["chart_id"]:
                self._index = i
                self._refresh()
                return False
        self._charts.append(chart)
        self._index = len(self._charts) - 1
        # A live chart is the turn's headline output -- surface it expanded.
        self.toggle.setChecked(True)
        self._refresh()
        return True

    def clear(self) -> None:
        """Case switch: charts are per-Case state (ITEM B discipline)."""
        self._charts = []
        self._index = 0
        self._refresh()

    # -- internals ------------------------------------------------------------ #

    def _toggle_body(self) -> None:
        self.body.setVisible(self.toggle.isChecked())

    def _step(self, delta: int) -> None:
        if not self._charts:
            return
        self._index = max(0, min(len(self._charts) - 1, self._index + delta))
        self._refresh()

    def _refresh(self) -> None:
        n = len(self._charts)
        self.setVisible(n > 0)
        self.toggle.setText(f"Charts ({n})")
        paging = n > 1
        self.prev_btn.setVisible(paging)
        self.next_btn.setVisible(paging)
        self.pos_label.setVisible(paging)
        if paging:
            self.pos_label.setText(f"{self._index + 1}/{n}")
            self.prev_btn.setEnabled(self._index > 0)
            self.next_btn.setEnabled(self._index < n - 1)
        self.body.setVisible(self.toggle.isChecked() and n > 0)
        if self._card is not None:
            self._body_lay.removeWidget(self._card)
            self._card.deleteLater()
            self._card = None
        self.last_render_summary = None
        if n == 0:
            self.caption_label.setVisible(False)
            return
        chart = self._charts[self._index]
        self._card = self._build_card(chart)
        self._body_lay.insertWidget(0, self._card)
        caption = chart.get("caption")
        self.caption_label.setText(caption if isinstance(caption, str) else "")
        self.caption_label.setVisible(bool(caption))

    def _build_card(self, chart: dict) -> QWidget:
        """The rendered chart widget -- a matplotlib canvas, or the honest
        text fallback when matplotlib is unavailable in this QGIS python."""
        if not matplotlib_available():
            self.last_render_summary = {"fallback": True}
            fallback = QLabel(
                f"{chart.get('title') or chart.get('chart_id')}\n"
                f"(chart not rendered: matplotlib unavailable -- "
                f"{_MATPLOTLIB_ERROR})"
            )
            fallback.setWordWrap(True)
            if self._block_style:
                fallback.setStyleSheet(self._block_style)
            return fallback
        figure = Figure(figsize=(4.0, _CANVAS_HEIGHT / 100.0), dpi=100)
        canvas = FigureCanvasQTAgg(figure)
        canvas.setFixedHeight(_CANVAS_HEIGHT)
        canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        spec = chart.get("vega_lite_spec") or {}
        self.last_render_summary = render_spec(figure, spec)
        canvas.draw()
        return canvas
