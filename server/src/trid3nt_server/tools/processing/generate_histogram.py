"""``generate_histogram``: 10-bin distribution chart of a raster's sampled
cells or a vector layer's numeric property -> chart-emission payload.

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

__all__ = ["generate_histogram"]

logger = logging.getLogger("trid3nt_server.tools.processing.generate_histogram")


#: Number of histogram bins (matches charts_common._summarize_raster's 10-bin convention).
_HIST_BINS = 10

# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_HISTOGRAM_META = AtomicToolMetadata(
    name="generate_histogram",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)

def _histogram_bins(values: np.ndarray, bins: int = _HIST_BINS) -> list[dict[str, Any]]:
    """Compute histogram bins as a list of inline Vega-Lite data rows."""
    if values.size == 0:
        raise ChartToolError("NO_DATA", "Layer has zero valid values to histogram.")
    counts, edges = np.histogram(values, bins=bins)
    return [
        {
            "bin_start": float(edges[i]),
            "bin_end": float(edges[i + 1]),
            "bin_label": f"{edges[i]:.3g}-{edges[i + 1]:.3g}",
            "count": int(counts[i]),
        }
        for i in range(len(counts))
    ]

# ---------------------------------------------------------------------------
# Tool 1: generate_histogram
# ---------------------------------------------------------------------------


@register_tool(
    _HISTOGRAM_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def generate_histogram(
    layer_uri: str,
    property: str | None = None,
    *,
    _storage_client: object | None = None,
    _created_turn_id: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Generate a histogram chart of a layer's values.

    Use this when the user asks to *see the distribution* of a layer: "show me
    a histogram of flood depths", "what does the damage-ratio distribution look
    like", "chart the population density".

    Raster layers: histograms the cell values (random-sampled to ~500k cells
    for large rasters; the distribution shape is preserved). Vector layers:
    histograms a numeric attribute (``property``, or the first numeric column
    if omitted).

    Do NOT use this for: a numeric answer ("how many / how much" - use
    spatial_query); a damage-state bar chart (use
    generate_damage_distribution); rendering the layer on the map (use
    publish_layer).

    Parameters:
        layer_uri: gs:// URI or local path of a raster (GeoTIFF/COG) or vector
            (GeoJSON/FlatGeobuf/GeoPackage) layer.
        property: for vector layers, the numeric attribute to histogram. Ignored
            for rasters. When omitted on a vector layer, the first numeric
            column is used.

    Returns:
        A ChartEmissionPayload dict (envelope_type="chart-emission") carrying a
        Vega-Lite v5 bar-chart spec with the binned counts inline, a title, and
        a one-line caption. The agent loop emits this as a chart-emission
        envelope and feeds a compact summary back for narration.

    Raises:
        ChartToolError: typed error_code (LAYER_OPEN_FAILED, PROPERTY_NOT_FOUND,
            NO_NUMERIC_PROPERTY, NO_DATA, DOWNLOAD_FAILED).
    """
    uri = _validate_uri(layer_uri, "layer_uri")
    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri(uri, tmpdir, "layer", _storage_client)
        ltype = _layer_type(local)
        if ltype == "raster":
            values = _sample_raster_values(local)
            prop_label = "value"
            n = int(values.size)
        else:
            gdf = _read_geodataframe(local)
            prop = _pick_property(gdf, property)
            series = gdf[prop].dropna()
            values = np.asarray(series.values, dtype=np.float64)
            values = values[np.isfinite(values)]
            prop_label = prop
            n = int(values.size)

    bins = _histogram_bins(values)
    vmin = float(np.min(values))
    vmax = float(np.max(values))
    vmean = float(np.mean(values))

    spec = {
        "title": f"Distribution of {prop_label}",
        "data": {"values": bins},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {
                "field": "bin_label",
                "type": "ordinal",
                "title": prop_label,
                "sort": None,
            },
            "y": {"field": "count", "type": "quantitative", "title": "count"},
        },
        "width": "container",
    }
    caption = (
        f"{n:,} values · min {vmin:.3g} · mean {vmean:.3g} · max {vmax:.3g} "
        f"· {_HIST_BINS} bins"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title=f"Histogram - {prop_label}",
        caption=caption,
        source_layer_uri=uri,
        created_turn_id=_created_turn_id,
    )
