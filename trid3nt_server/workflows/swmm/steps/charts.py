"""The one multi-series line chart the SWMM family draws.

Every SWMM template's product is the same picture: several named series against
elapsed time, the series name as the legend, and the KNOB as the thing the legend
distinguishes (with groundwater vs without, snowmelt vs rain-only, RDII vs direct
runoff). The spec is the product and the plugin dock is the renderer, so what is
shared is the SPEC shape, not a figure.

Honesty floor: fewer than two plottable points is no chart, never an empty one.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["line_chart_spec"]


def line_chart_spec(
    *,
    title: str,
    series: Mapping[str, Sequence[tuple[float, float]]],
    x_title: str,
    y_title: str,
    x_field: str = "t_hr",
    y_field: str = "value",
    color_title: str = "",
    x_round: int = 3,
    y_round: int | None = 5,
    ref_line: tuple[float, str] | None = None,
) -> dict[str, Any] | None:
    """A Vega-Lite multi-series line spec, or ``None`` when there is nothing to draw.

    ``y_round=None`` leaves the values as the caller supplied them, which is what
    a step that already rounded its reported series wants - rounding twice would
    make the chart disagree with the scalars beside it.

    ``ref_line`` layers a horizontal rule (a target depth, a threshold); it turns
    the spec into a layered one, so it is only paid for when asked for.
    """
    values: list[dict[str, Any]] = []
    for name, points in series.items():
        for x, y in points:
            values.append({
                x_field: round(float(x), x_round),
                y_field: float(y) if y_round is None else round(float(y), y_round),
                "series": name,
            })
    if len(values) < 2:
        return None

    encoding = {
        "x": {"field": x_field, "type": "quantitative", "title": x_title},
        "y": {"field": y_field, "type": "quantitative", "title": y_title},
        "color": {"field": "series", "type": "nominal", "title": color_title},
    }
    if ref_line is None:
        return {"title": title, "data": {"values": values},
                "mark": {"type": "line"}, "encoding": encoding}

    ref_value, ref_label = ref_line
    return {
        "title": title,
        "data": {"values": values},
        "layer": [
            {"mark": {"type": "line"}, "encoding": encoding},
            {
                "data": {"values": [{"ref": float(ref_value), "label": ref_label}]},
                "mark": {"type": "rule", "strokeDash": [4, 4], "color": "#888"},
                "encoding": {"y": {"field": "ref", "type": "quantitative"}},
            },
        ],
    }
