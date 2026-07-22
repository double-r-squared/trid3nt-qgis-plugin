"""``generate_damage_distribution``: Pelicun damage-state distribution bar
chart from a per-asset damage vector layer -> chart-emission payload.

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
    _VEGA_LITE_V5_SCHEMA,
    _download_uri_bytes,
    _materialize_uri,
    _read_geodataframe,
    _validate_uri,
    build_chart_payload,
)

__all__ = ["generate_damage_distribution"]

logger = logging.getLogger("trid3nt_server.tools.processing.generate_damage_distribution")


# Pelicun DS labels (mirrors postprocess_pelicun._DS_LABELS - the canonical
# DamageStateKey ordering). ds_mean is binned int(round(ds_mean)).clip(0,4).
_DS_LABELS: tuple[str, ...] = (
    "DS0_none",
    "DS1_slight",
    "DS2_moderate",
    "DS3_extensive",
    "DS4_complete",
)

_DS_DISPLAY: tuple[str, ...] = (
    "DS0 None",
    "DS1 Slight",
    "DS2 Moderate",
    "DS3 Extensive",
    "DS4 Complete",
)

_DAMAGE_DIST_META = AtomicToolMetadata(
    name="generate_damage_distribution",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)

# ---------------------------------------------------------------------------
# Tool 4: generate_damage_distribution
# ---------------------------------------------------------------------------


@register_tool(
    _DAMAGE_DIST_META,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def generate_damage_distribution(
    damage_layer_uri: str,
    *,
    _storage_client: object | None = None,
    _created_turn_id: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Generate a Pelicun damage-state distribution bar chart.

    Use this after a Pelicun damage run (run_pelicun_damage_assessment), when
    the user asks to *see the damage breakdown*: "show me the damage
    distribution", "chart how many structures are in each damage state", "what's
    the damage-state breakdown".

    Reads the per-asset FlatGeobuf's ``ds_mean`` column (the same column
    postprocess_pelicun aggregates), bins each structure into DS0..DS4 via
    int(round(ds_mean)).clip(0,4), and charts the per-state structure counts.

    Do NOT use this for: a generic numeric histogram (use generate_histogram);
    portfolio loss totals (use postprocess_pelicun / compute_impact_envelope);
    counting structures above a damage threshold (use spatial_query).

    Parameters:
        damage_layer_uri: gs:// URI or local path to the FlatGeobuf returned by
            run_pelicun_damage_assessment (must carry a ``ds_mean`` column).

    Returns:
        A ChartEmissionPayload dict with a Vega-Lite v5 bar chart of DS0..DS4
        structure counts, a title, and a caption with the total damaged count.

    Raises:
        ChartToolError: MISSING_DAMAGE_COLUMN if ``ds_mean`` is absent; NO_DATA
            on zero features; LAYER_OPEN_FAILED / DOWNLOAD_FAILED on read failure.
    """
    uri = _validate_uri(damage_layer_uri, "damage_layer_uri")
    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_uri(uri, tmpdir, "damage", _storage_client)
        gdf = _read_geodataframe(local)

    if len(gdf) == 0:
        raise ChartToolError("NO_DATA", "Damage layer has zero features.")
    if "ds_mean" not in gdf.columns:
        raise ChartToolError(
            "MISSING_DAMAGE_COLUMN",
            "Damage layer is missing the 'ds_mean' column produced by "
            "run_pelicun_damage_assessment. Available columns: "
            f"{sorted(str(c) for c in gdf.columns if c != 'geometry')}",
        )

    ds_mean = np.asarray(gdf["ds_mean"], dtype=np.float64)
    ds_mean = ds_mean[np.isfinite(ds_mean)]
    if ds_mean.size == 0:
        raise ChartToolError("NO_DATA", "ds_mean column has no finite values.")

    modal = np.round(ds_mean).clip(0, 4).astype(int)
    counts = {label: 0 for label in _DS_LABELS}
    unique, freq = np.unique(modal, return_counts=True)
    for u, f in zip(unique.tolist(), freq.tolist()):
        counts[_DS_LABELS[int(u)]] = int(f)

    rows = [
        {
            "damage_state": _DS_DISPLAY[i],
            "ds_key": _DS_LABELS[i],
            "ds_index": i,
            "count": counts[_DS_LABELS[i]],
        }
        for i in range(len(_DS_LABELS))
    ]
    n_total = int(ds_mean.size)
    n_damaged = int((ds_mean >= 1.0).sum())

    spec = {
        "title": "Damage-state distribution",
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {
                "field": "damage_state",
                "type": "ordinal",
                "title": "damage state",
                "sort": [d for d in _DS_DISPLAY],
            },
            "y": {"field": "count", "type": "quantitative", "title": "structures"},
            "color": {
                "field": "ds_index",
                "type": "ordinal",
                "scale": {"scheme": "yellorred"},
                "legend": None,
            },
        },
        "width": "container",
    }
    caption = (
        f"{n_total:,} structures · {n_damaged:,} damaged (DS1+) "
        f"· {counts['DS4_complete']:,} destroyed (DS4)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Damage-state distribution",
        caption=caption,
        source_layer_uri=uri,
        created_turn_id=_created_turn_id,
    )
