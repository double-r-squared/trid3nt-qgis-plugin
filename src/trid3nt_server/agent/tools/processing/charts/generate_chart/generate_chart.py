"""``generate_chart``: the ONE generic interactive-chart primitive.

Wraps ``charts_common.build_chart_payload`` so the LLM can render ANY
Vega-Lite v5 chart (histogram, time series, damage-state bars, choropleth
class breaks, scatter, heatmap, ...) from a caller-composed spec. It replaces
the four fixed-shape ``generate_*`` chart tools: the SHAPE of the chart is now
the caller's Vega-Lite spec, and the binning/classification that those tools
hard-coded is composed in the python playground (``code_exec_request`` /
``spatial_query``) then passed here as inline ``records``.

Interactivity is guaranteed BY CONSTRUCTION: every mark is normalized to carry
``tooltip: true`` and image (rasterized-PNG) marks are rejected, so this tool
can never emit the non-interactive matplotlib->PNG shape the playground would.
The emitted payload is the same ``ChartEmissionPayload`` envelope the plugin
chart panel already renders, so no client change is needed.
"""

from __future__ import annotations

import logging
import tempfile
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.processing.charts_common import (
    ChartToolError,
    _MAX_ROWS,
    _layer_type,
    _materialize_uri,
    _read_geodataframe,
    _sample_raster_values,
    _validate_uri,
    build_chart_payload,
)

__all__ = ["generate_chart"]

logger = logging.getLogger("trid3nt_server.agent.tools.processing.generate_chart.generate_chart")


_GENERATE_CHART_META = AtomicToolMetadata(
    name="generate_chart",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)

#: Vega-Lite spec keys that can nest sub-views carrying their own ``mark``.
_VIEW_CONTAINER_KEYS = ("layer", "hconcat", "vconcat", "concat", "spec")


def _normalize_mark(view: dict[str, Any]) -> None:
    """Force ``mark.tooltip = true`` on a single-view ``view``; reject image marks.

    Converts a string mark to its dict form and stamps ``tooltip`` so every
    emitted chart is interactive. Raises on ``mark.type == "image"`` (the
    rasterized-PNG shape the interactive contract exists to exclude).
    """
    mark = view.get("mark")
    if mark is None:
        return
    if isinstance(mark, str):
        mark = {"type": mark}
    elif isinstance(mark, dict):
        mark = dict(mark)
    else:
        return
    if str(mark.get("type", "")).lower() == "image":
        raise ChartToolError(
            "IMAGE_MARK_REJECTED",
            "generate_chart emits INTERACTIVE Vega-Lite charts; an 'image' "
            "(rasterized PNG) mark is not allowed. Use a real mark (bar / line "
            "/ point / rect / area / rule) with encodings.",
        )
    # Force interactivity: stamp tooltip=true unless the caller already supplied a
    # truthy tooltip (True or a richer {"content": ...} config, which we preserve).
    if not mark.get("tooltip"):
        mark["tooltip"] = True
    view["mark"] = mark


def _ensure_interactive(spec: dict[str, Any]) -> bool:
    """Recursively stamp ``tooltip`` on every mark in ``spec``.

    Returns True if at least one mark was found anywhere in the (possibly
    layered / concatenated / faceted) spec, so the caller can reject a
    mark-less spec (nothing to draw).
    """
    found = False
    if "mark" in spec:
        _normalize_mark(spec)
        found = True
    for key in _VIEW_CONTAINER_KEYS:
        child = spec.get(key)
        if isinstance(child, dict):
            found = _ensure_interactive(child) or found
        elif isinstance(child, list):
            for sub in child:
                if isinstance(sub, dict):
                    found = _ensure_interactive(sub) or found
    return found


def _records_from_layer(layer_uri: str, storage_client: object | None) -> list[dict[str, Any]]:
    """Read a layer's tabular rows for inline chart data.

    Vector layer -> one row per feature (attributes, geometry dropped).
    Raster layer -> one ``{"value": <cell>}`` row per sampled valid cell.
    Capped at ``_MAX_ROWS`` (binning/classification is the caller's job in the
    playground; this is raw-row injection only).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri(layer_uri, tmpdir, "layer", storage_client)
        if _layer_type(local) == "raster":
            values = _sample_raster_values(local)
            rows = [{"value": float(v)} for v in values[:_MAX_ROWS]]
        else:
            gdf = _read_geodataframe(local)
            cols = [c for c in gdf.columns if c != "geometry"]
            rows = [
                {c: _json_safe(row[c]) for c in cols}
                for _, row in gdf.head(_MAX_ROWS).iterrows()
            ]
    if not rows:
        raise ChartToolError("NO_DATA", f"Layer {layer_uri!r} yielded zero chartable rows.")
    return rows


def _json_safe(value: Any) -> Any:
    """Coerce a geopandas cell to a JSON-serializable scalar for the inline spec."""
    import math

    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return str(value)


@register_tool(
    _GENERATE_CHART_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def generate_chart(
    vega_lite_spec: dict[str, Any],
    title: str,
    caption: str | None = None,
    layer_uri: str | None = None,
    records: list[dict[str, Any]] | None = None,
    *,
    _storage_client: object | None = None,
    _created_turn_id: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Render an interactive Vega-Lite chart from a caller-composed spec.

    Use this for ANY chart the user asks to SEE: a histogram / value
    distribution, a value-vs-time series, a Pelicun damage-state (DS0..DS4)
    bar chart, a choropleth class-break legend, a scatter, a heatmap. Compose
    the bins/classes/series in the python playground (code_exec_request /
    spatial_query), then pass them here as ``records`` with an ``encoding``.

    Do NOT use for: a numeric answer (spatial_query); rendering a layer on the
    map (publish_layer).

    Parameters:
        vega_lite_spec: a Vega-Lite v5 spec dict. Supply the ``mark`` (bar /
            line / point / rect / area / rule) + ``encoding``; a ``$schema`` is
            injected and every mark is forced interactive (``tooltip: true``).
            An ``image``-mark spec is rejected. Inline ``data.values`` may be
            supplied here directly, or via ``records`` / ``layer_uri`` below.
        title: chart title (non-empty).
        caption: optional one-line interpretation under the chart.
        records: inline data rows (list of flat dicts). Injected as the spec's
            ``data.values`` (top-level, inherited by layers). Takes precedence
            over ``layer_uri``. Capped at 2000 rows.
        layer_uri: s3:// / local layer to pull inline rows from when ``records``
            is absent - vector attributes (one row per feature) or sampled
            raster cell values (``{"value": ...}``). Capped at 2000 rows.

    Returns:
        A ChartEmissionPayload dict (envelope_type="chart-emission") the agent
        loop emits as a chart-emission envelope; a compact summary is fed back.

    Raises:
        ChartToolError: NO_MARK (spec has no mark to draw), IMAGE_MARK_REJECTED,
            NO_DATA, LAYER_OPEN_FAILED / DOWNLOAD_FAILED on a layer read.
    """
    if not isinstance(vega_lite_spec, dict):
        raise ChartToolError(
            "NO_MARK",
            f"vega_lite_spec must be a dict Vega-Lite spec; got {type(vega_lite_spec).__name__}.",
        )
    if not isinstance(title, str) or not title.strip():
        raise ChartToolError("NO_DATA", "title must be a non-empty string.")

    spec = dict(vega_lite_spec)

    # --- Resolve inline data: explicit records win over a layer read. ---------
    resolved: list[dict[str, Any]] | None = None
    source_uri: str | None = None
    if records is not None:
        if not isinstance(records, list):
            raise ChartToolError(
                "NO_DATA", f"records must be a list of row dicts; got {type(records).__name__}."
            )
        resolved = [r for r in records if isinstance(r, dict)]
    elif layer_uri is not None:
        source_uri = _validate_uri(layer_uri, "layer_uri")
        resolved = _records_from_layer(source_uri, _storage_client)

    if resolved is not None:
        spec["data"] = {"values": resolved}

    # --- Guarantee an interactive, drawable spec. -----------------------------
    if not _ensure_interactive(spec):
        raise ChartToolError(
            "NO_MARK",
            "vega_lite_spec has no 'mark' to draw (no top-level mark and no "
            "layer/concat sub-view with a mark). Add a mark + encoding.",
        )

    return build_chart_payload(
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=source_uri,
        created_turn_id=_created_turn_id,
    )
