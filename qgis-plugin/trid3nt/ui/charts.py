"""The pure Vega-Lite -> matplotlib chart renderer for the TRID3NT plugin
(live-feedback 2026-07-13, OpenQuake result parity; charts-window 2026-08-04).

The web UI renders the agent's ``chart-emission`` payloads (Vega-Lite v5
specs -- contracts ``chart_contracts.ChartEmissionPayload``) as inline chart
cards; the QGIS plugin renders the same payloads with the small interpreter
below:

* ``parse_chart_payload`` / spec helpers -- defensive, pure-python handling
  of the wire payload (chart_id + title + caption + vega_lite_spec).
* ``render_spec`` -- a deliberately SMALL Vega-Lite interpreter that draws
  the subset our agent actually emits (see ``chart_tools.py``: line+point,
  dashed rule reference lines, bar, rect/heatmap) onto a matplotlib Figure.
  It is NOT a general Vega renderer; unknown marks are skipped and counted,
  never crashed on -- a malformed persisted spec must not break a case open.

The interactive surface that HOSTS ``render_spec`` is ``charts_window
.ChartsWindow`` -- the bottom-docked TUFLOW-Viewer-style window (NATE
charts-window directive 2026-08-04). This module is the renderer only; it
carries no Qt widgets of its own.

Rendering choice (researched 2026-07-13): matplotlib ``FigureCanvasQTAgg``
embedded in the dock. Debian QGIS 3.40 ships matplotlib (3.10) in the same
system python as PyQt5, the QtAgg backend binds to the already-imported
qgis.PyQt binding, and it gives log-log axes / legends / dashed rules for
free -- the hazard curve is log-log, which pure-QPainter code would have to
hand-roll. GEM's IRMT plugin was rejected (not installed, its viewer is
coupled to OQ-engine NRML outputs, not our Vega payloads); a server-side PNG
render was rejected (server change + restart + flood smoke for zero offline
benefit). matplotlib import is GUARDED: when absent the window degrades to a guided
fix panel (what's missing, why, the exact per-OS pip command, an "Attempt
install" button) -- see ``charts_window.MissingMatplotlibPanel`` -- never a
crash. QGIS 4's macOS/Windows bundles dropped matplotlib (QGIS 3 shipped it);
the guard + panel below are what makes that survivable offline.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Dict, List, Optional

from .. import install_dependencies

# -- guarded matplotlib import (see module docstring) ------------------------ #
# ``Figure`` + a Qt canvas class, no pyplot (pyplot owns global backend state
# we must not fight QGIS for). backend_qtagg resolves its binding via the
# already-imported qgis.PyQt (PyQt5); backend_qt5agg is the pre-3.5 fallback.
# The check itself is cheap (one import attempt) and CACHED via
# ``_MATPLOTLIB_CHECKED`` -- every chart-emission frame and every dock open
# calls ``matplotlib_available()``, so a repeated failing import must not
# re-walk sys.path on each one. ``recheck_matplotlib`` is the explicit
# cache-bust, used after the panel's "Attempt install" succeeds.
Figure = None  # type: ignore[assignment]
FigureCanvasQTAgg = None  # type: ignore[assignment]
_MATPLOTLIB_ERROR: Optional[str] = None
_MATPLOTLIB_CHECKED = False


def _do_matplotlib_check() -> None:
    global Figure, FigureCanvasQTAgg, _MATPLOTLIB_ERROR, _MATPLOTLIB_CHECKED
    try:  # noqa: SIM105
        from matplotlib.figure import Figure as _Figure

        try:
            from matplotlib.backends.backend_qtagg import (
                FigureCanvasQTAgg as _Canvas,
            )
        except ImportError:  # older matplotlib
            from matplotlib.backends.backend_qt5agg import (
                FigureCanvasQTAgg as _Canvas,
            )
        Figure = _Figure
        FigureCanvasQTAgg = _Canvas
        _MATPLOTLIB_ERROR = None
    except Exception as _exc:  # noqa: BLE001 -- absence is a supported state
        Figure = None
        FigureCanvasQTAgg = None
        _MATPLOTLIB_ERROR = f"{type(_exc).__name__}: {_exc}"
    _MATPLOTLIB_CHECKED = True


def matplotlib_available() -> bool:
    if not _MATPLOTLIB_CHECKED:
        _do_matplotlib_check()
    return _MATPLOTLIB_ERROR is None


def matplotlib_error() -> Optional[str]:
    """The cached import failure string, or None once available. Always
    forces the (cached) check first so a caller that never called
    ``matplotlib_available()`` still gets an answer."""
    matplotlib_available()
    return _MATPLOTLIB_ERROR


def recheck_matplotlib() -> bool:
    """Bust the cache and retry the import (post "Attempt install" -- pip
    dropped fresh files onto sys.path that the cached failure predates)."""
    import importlib

    importlib.invalidate_caches()
    global _MATPLOTLIB_CHECKED
    _MATPLOTLIB_CHECKED = False
    return matplotlib_available()


# --------------------------------------------------------------------------- #
# Per-OS "how do I get matplotlib into THIS interpreter" command builder.
# Pure (no Qt, no subprocess) -- the panel's Copy/Attempt-install actions and
# the tests both call through this. Never hardcodes a path: QGIS embeds its
# own python per-OS (macOS: <QGIS.app>/Contents/MacOS/bin/python3, a wrapper
# the running ``sys.executable`` is NOT; Linux/Windows: the running
# interpreter already IS the real, invokable one). The executable-resolution
# logic itself lives in ``install_dependencies`` (one source of truth shared
# with the standalone script and its "Attempt install" QProcess target) --
# this module only re-exports it under its established name.
# --------------------------------------------------------------------------- #


def install_python_executable(
    platform: Optional[str] = None,
    exec_prefix: Optional[str] = None,
    executable: Optional[str] = None,
) -> str:
    """The python binary that must run ``install_dependencies.py`` to land
    matplotlib where this QGIS's interpreter will find it -- derived from
    ``sys.exec_prefix`` / ``sys.executable`` at call time, never a baked-in
    path."""
    return install_dependencies.install_python_executable(
        platform, exec_prefix, executable
    )


def install_command_argv(
    platform: Optional[str] = None,
    exec_prefix: Optional[str] = None,
    executable: Optional[str] = None,
) -> List[str]:
    """The argv ``QProcess`` runs: ``[python, install_dependencies.py]`` --
    the shared script (not raw pip) so the check/install/re-verify logic has
    exactly one implementation."""
    py = install_python_executable(platform, exec_prefix, executable)
    return [py, install_dependencies.__file__]


def install_command_str(
    platform: Optional[str] = None,
    exec_prefix: Optional[str] = None,
    executable: Optional[str] = None,
) -> str:
    """The human-facing / copy-button command line for the same argv."""
    py, script = install_command_argv(platform, exec_prefix, executable)
    quoted_py = f'"{py}"' if " " in py else py
    quoted_script = f'"{script}"' if " " in script else script
    return " ".join([quoted_py, quoted_script])


# Default series colors (matplotlib tab10 order) for color-field grouping.
_SERIES_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


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
    """A plottable float, or None. Rejects bools AND non-finite (NaN/inf) --
    a persisted spec carrying NaN/inf must not reach the matplotlib axis: it
    poisons the auto-range into a degenerate (non-finite) extent, and the same
    degenerate extent is what feeds a native precision computation elsewhere.
    Non-finite points are dropped, exactly like non-numeric ones."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


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
