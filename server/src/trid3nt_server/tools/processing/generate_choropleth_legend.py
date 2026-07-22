"""``generate_choropleth_legend``: quantile class-break legend chart for a
vector layer's numeric property -> chart-emission payload.

Carved out of the original multi-tool ``chart_tools`` module (job-0230) in the
tools/ reorg; behavior and the registered tool surface are unchanged. The
shared chart-emission core lives in
``trid3nt_server.tools.processing.charts_common``.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import numpy as np

from trid3nt_contracts import new_ulid
from trid3nt_contracts.chart_contracts import ChartEmissionPayload
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.processing.charts_common import (
    ChartToolError,
    _MAX_ROWS,
    _RASTER_EXTS,
    _RASTER_SAMPLE_CAP,
    _SAMPLE_SEED,
    _VECTOR_EXTS,
    _VEGA_LITE_V5_SCHEMA,
    _download_uri_bytes,
    _layer_type,
    _materialize_uri,
    _numeric_columns,
    _pick_property,
    _read_geodataframe,
    _sample_raster_values,
    _validate_uri,
    build_chart_payload,
)

__all__ = ["generate_choropleth_legend"]

logger = logging.getLogger("trid3nt_server.tools.processing.generate_choropleth_legend")


_CHOROPLETH_META = AtomicToolMetadata(
    name="generate_choropleth_legend",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)

# ---------------------------------------------------------------------------
# Tool 2: generate_choropleth_legend
# ---------------------------------------------------------------------------


@register_tool(
    _CHOROPLETH_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def generate_choropleth_legend(
    layer_uri: str,
    property: str | None = None,
    *,
    _storage_client: object | None = None,
    _created_turn_id: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Generate a class-break summary chart for a layer's choropleth style.

    Use this when the user wants the *legend / class breakdown* behind a
    choropleth-styled vector layer: "summarize the choropleth classes", "how
    many features fall in each class", "show me the legend distribution".

    Computes quantile class breaks (5 classes) over a numeric property and
    counts the features in each class - the bar chart mirrors what a choropleth
    legend communicates: which value-ranges hold how much of the data.

    Do NOT use this for: a raw value histogram (use generate_histogram); a
    Pelicun damage-state chart (use generate_damage_distribution); rendering the
    styled layer (use publish_layer).

    Parameters:
        layer_uri: gs:// URI or local path of a vector layer (GeoJSON /
            FlatGeobuf / GeoPackage).
        property: numeric attribute the choropleth is keyed on. When omitted the
            first numeric column is used.

    Returns:
        A ChartEmissionPayload dict with a Vega-Lite v5 bar chart of per-class
        feature counts (5 quantile classes), a title, and a caption naming the
        class breaks.

    Raises:
        ChartToolError: LAYER_OPEN_FAILED, PROPERTY_NOT_FOUND,
            NO_NUMERIC_PROPERTY, NO_DATA, DOWNLOAD_FAILED.
    """
    uri = _validate_uri(layer_uri, "layer_uri")
    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri(uri, tmpdir, "layer", _storage_client)
        ltype = _layer_type(local)
        if ltype == "raster":
            # A choropleth is a vector concept; for a raster we class-break the
            # sampled cell values (the same quantile logic).
            values = _sample_raster_values(local)
            prop_label = "value"
        else:
            gdf = _read_geodataframe(local)
            prop = _pick_property(gdf, property)
            series = gdf[prop].dropna()
            values = np.asarray(series.values, dtype=np.float64)
            values = values[np.isfinite(values)]
            prop_label = prop

    if values.size == 0:
        raise ChartToolError("NO_DATA", "Layer has zero valid values to class-break.")

    n_classes = min(5, max(1, int(np.unique(values).size)))
    # Quantile class breaks (the standard choropleth "quantile" classification).
    quantiles = np.linspace(0.0, 1.0, n_classes + 1)
    edges = np.unique(np.quantile(values, quantiles))
    # Guard degenerate (all-equal) data - fall back to a single class.
    if edges.size < 2:
        edges = np.array([float(values.min()), float(values.max()) + 1.0])

    rows: list[dict[str, Any]] = []
    for i in range(len(edges) - 1):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == len(edges) - 2:
            in_class = (values >= lo) & (values <= hi)
        else:
            in_class = (values >= lo) & (values < hi)
        rows.append(
            {
                "class_index": i,
                "class_label": f"{lo:.3g}-{hi:.3g}",
                "break_low": lo,
                "break_high": hi,
                "count": int(in_class.sum()),
            }
        )

    spec = {
        "title": f"Choropleth classes - {prop_label}",
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {
                "field": "class_label",
                "type": "ordinal",
                "title": f"{prop_label} class",
                "sort": None,
            },
            "y": {"field": "count", "type": "quantitative", "title": "feature count"},
            "color": {
                "field": "class_index",
                "type": "ordinal",
                "scale": {"scheme": "blues"},
                "legend": None,
            },
        },
        "width": "container",
    }
    caption = (
        f"{len(rows)} quantile classes over {prop_label} "
        f"· {int(values.size):,} features"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title=f"Choropleth legend - {prop_label}",
        caption=caption,
        source_layer_uri=uri,
        created_turn_id=_created_turn_id,
    )
