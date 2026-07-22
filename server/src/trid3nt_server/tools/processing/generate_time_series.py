"""``generate_time_series``: time-vs-value line chart from a raster stack's
band dimension or a vector layer's time column -> chart-emission payload.

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
    _VECTOR_EXTS,
    _VEGA_LITE_V5_SCHEMA,
    _download_uri_bytes,
    _layer_type,
    _materialize_uri,
    _numeric_columns,
    _read_geodataframe,
    _validate_uri,
    build_chart_payload,
)

__all__ = ["generate_time_series"]

logger = logging.getLogger("trid3nt_server.tools.processing.generate_time_series")


_TIME_SERIES_META = AtomicToolMetadata(
    name="generate_time_series",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)

# ---------------------------------------------------------------------------
# Tool 3: generate_time_series
# ---------------------------------------------------------------------------


def _detect_raster_time_dim(local_path: str) -> list[Any] | None:
    """Return per-band time labels for a temporal raster, or None.

    A temporal raster is detected when the band count > 1 AND the raster carries
    band descriptions / time tags. We read per-band means as the series value.
    Returns a list of (label, mean) when temporal, else None.
    """
    try:
        import rasterio
    except ImportError as exc:
        raise ChartToolError("LAYER_OPEN_FAILED", "rasterio not available") from exc

    try:
        with rasterio.open(local_path) as src:
            band_count = src.count
            if band_count <= 1:
                return None
            descriptions = list(src.descriptions or [])
            tags = src.tags()
            nodata = src.nodata
            # Heuristic: a multiband raster is "temporal" if it declares band
            # descriptions OR a NETCDF/time tag. A plain RGB(A) raster (3-4
            # bands, no descriptions) is NOT temporal.
            has_time_tag = any(
                "time" in str(k).lower() or "date" in str(k).lower() for k in tags
            )
            has_descriptions = any(d for d in descriptions)
            if not (has_time_tag or has_descriptions):
                return None
            series: list[dict[str, Any]] = []
            for b in range(1, band_count + 1):
                band = src.read(b).astype(np.float64)
                if nodata is not None and not (
                    isinstance(nodata, float) and math.isnan(nodata)
                ):
                    valid = band[(band != nodata) & np.isfinite(band)]
                else:
                    valid = band[np.isfinite(band)]
                label = (
                    descriptions[b - 1]
                    if b - 1 < len(descriptions) and descriptions[b - 1]
                    else f"t{b}"
                )
                series.append(
                    {
                        "time": str(label),
                        "value": float(np.mean(valid)) if valid.size else 0.0,
                    }
                )
            return series
    except ChartToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED", f"Could not open raster {local_path!r}: {exc}"
        ) from exc

_TIME_COLUMN_CANDIDATES = (
    "time",
    "date",
    "datetime",
    "timestamp",
    "observed_at",
    "obs_time",
    "valid_time",
    "year",
)

def _detect_vector_time_dim(gdf: Any) -> tuple[str, str] | None:
    """Return (time_col, value_col) for a temporal vector layer, or None."""
    cols_lower = {str(c).lower(): c for c in gdf.columns if c != "geometry"}
    time_col = None
    for cand in _TIME_COLUMN_CANDIDATES:
        if cand in cols_lower:
            time_col = cols_lower[cand]
            break
    if time_col is None:
        return None
    numeric = [c for c in _numeric_columns(gdf) if c != time_col]
    if not numeric:
        return None
    return time_col, numeric[0]

@register_tool(
    _TIME_SERIES_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def generate_time_series(
    layer_uri: str,
    *,
    _storage_client: object | None = None,
    _created_turn_id: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Generate a time-series line chart for a temporal layer.

    Use this when the user wants to see how a layer's value changes over time:
    "plot the discharge over time", "show the precipitation time series", "chart
    the trend".

    Works on temporal rasters (one band per timestep, with band descriptions or
    a time tag) and temporal vectors (a time/date column plus a numeric value
    column). The raster series is the per-band spatial mean; the vector series
    is the numeric column ordered by the time column.

    Do NOT use this for: a static (single-timestep) layer - it returns a
    NO_TIME_DIMENSION error envelope so the agent narrates honestly that the
    layer has no time dimension. For a value distribution use
    generate_histogram.

    Parameters:
        layer_uri: gs:// URI or local path of a temporal raster or vector layer.

    Returns:
        A ChartEmissionPayload dict with a Vega-Lite v5 line chart of value vs.
        time.

    Raises:
        ChartToolError: NO_TIME_DIMENSION when the layer carries no time
            dimension (this is the clean, expected "wrong tool" envelope - the
            agent loop feeds it back so Gemini picks a different chart);
            LAYER_OPEN_FAILED / DOWNLOAD_FAILED on read failure.
    """
    uri = _validate_uri(layer_uri, "layer_uri")
    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri(uri, tmpdir, "layer", _storage_client)
        ltype = _layer_type(local)
        if ltype == "raster":
            series = _detect_raster_time_dim(local)
            if series is None:
                raise ChartToolError(
                    "NO_TIME_DIMENSION",
                    "This raster has no time dimension (single band or no time "
                    "tags). Use generate_histogram for its value distribution.",
                )
            rows = series
            value_label = "mean value"
        else:
            gdf = _read_geodataframe(local)
            detected = _detect_vector_time_dim(gdf)
            if detected is None:
                raise ChartToolError(
                    "NO_TIME_DIMENSION",
                    "This vector layer has no time column (no time/date/datetime "
                    "attribute with a numeric value column). Use generate_histogram "
                    "or generate_choropleth_legend instead.",
                )
            time_col, value_col = detected
            sub = gdf[[time_col, value_col]].dropna()
            # Sort by time for a sane line chart.
            try:
                sub = sub.sort_values(time_col)
            except Exception:  # noqa: BLE001 - unsortable time; keep insertion order
                pass
            rows = [
                {"time": str(t), "value": float(v)}
                for t, v in zip(sub[time_col].tolist(), sub[value_col].tolist())
            ]
            value_label = value_col

    if not rows:
        raise ChartToolError("NO_DATA", "Temporal layer produced zero time-series rows.")

    spec = {
        "title": "Time series",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "time", "type": "ordinal", "title": "time", "sort": None},
            "y": {"field": "value", "type": "quantitative", "title": value_label},
        },
        "width": "container",
    }
    caption = f"{len(rows)} timesteps · {value_label}"
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Time series",
        caption=caption,
        source_layer_uri=uri,
        created_turn_id=_created_turn_id,
    )
