"""Chart-generation atomic tools (job-0230, sprint-13 Stage 2).

Four tools that turn an already-fetched layer into a **Vega-Lite v5 chart**
the web client can render inline (stacked preview) and in a full-viewport
gallery.  They are the agent-facing producers behind the conversational data
analysis layer (memory ``project_conversational_data_analysis_layer``):

    generate_histogram(layer_uri, property) -> ChartEmissionPayload dict
        Raster: cell-value histogram (random-sampled, cap ~500k cells).
        Vector: numeric-property histogram.

    generate_choropleth_legend(layer_uri) -> ChartEmissionPayload dict
        Class-break summary bar chart for a layer's active style (quantile
        class breaks over a numeric property, or category counts).

    generate_time_series(layer_uri) -> ChartEmissionPayload dict
        Temporal raster (band-per-timestep) / temporal vector (time column):
        a line chart of value-vs-time.  Clean typed error envelope if the
        layer carries no time dimension.

    generate_damage_distribution(damage_layer_uri) -> ChartEmissionPayload dict
        Pelicun damage-state distribution (DS0..DS4) bars, read from the
        per-asset FlatGeobuf ``ds_mean`` column (the same column
        ``postprocess_pelicun`` aggregates).

Contract boundary (job-0223)
----------------------------
Each tool computes chart **data** deterministically (rasterio / geopandas /
numpy - never an LLM call, Invariant 2), builds a Vega-Lite v5 JSON spec with
the data **inline** (``spec["data"]["values"]``, capped at ``_MAX_ROWS`` rows),
wraps it in a :class:`grace2_contracts.chart_contracts.ChartEmissionPayload`
(which structurally validates the spec - ``$schema`` present, see
``is_structurally_valid_vega_lite_spec``), and returns
``payload.model_dump(mode="json")`` as the tool result.

The agent loop (server.py ``_stream_gemini_reply``) detects a
chart-emission-shaped result, emits a ``chart-emission`` WS envelope to the
client AND feeds a **compact data summary** (NOT the full inline spec) back to
Gemini as the ``function_response`` for narration (adapter.py
``summarize_tool_result`` strips ``vega_lite_spec``).  It also persists a
``SessionChartRecord`` to the session document so the chart replays on Case
rehydration.

Determinism boundary (Invariant 1 / Decision H / FR-AS-7)
---------------------------------------------------------
Every number in the chart is a deterministic aggregate of the source layer's
pixels / features; the inline spec carries those numbers as structured data,
never narrated free text.  The agent's narration cites the same tool-computed
summary fed back as ``function_response``.  No cost field anywhere (Invariant 9).

Caching: ``ttl_class="dynamic-1h"``, ``source_class="chart_tools"``,
``cacheable=True`` (the FR-DC-6 metadata value; the only consistent pairing
with a non-``live-no-cache`` class).  In practice these tools do **not** route
their result through the GCS cache shim (same as ``postprocess_pelicun``,
which is ``cacheable=True`` but returns its envelope in-process): each call
mints a fresh ``chart_id`` ULID so the web client can key + de-dupe charts in
the gallery, and caching the payload would re-use a stale ``chart_id``.  The
expensive part (the layer read) is already cached upstream by the fetchers
that produced the layer.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import numpy as np

from grace2_contracts import new_ulid
from grace2_contracts.chart_contracts import ChartEmissionPayload
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "generate_histogram",
    "generate_choropleth_legend",
    "generate_time_series",
    "generate_damage_distribution",
    "ChartToolError",
    "build_chart_payload",
    "is_chart_emission_result",
    # Engine-output chart builders (task-198): turn the ALREADY-parsed
    # non-raster engine quantities into chart-emission payloads the composer
    # side-emits. They never read a layer / never call an LLM - they accept the
    # in-memory numbers the engine postprocess already computed.
    "build_hazard_curve_chart",
    "build_uhs_chart",
    "build_budget_partition_chart",
    "build_head_decline_chart",
    "build_head_series_chart",
    # Wave-5 saltwater intrusion cross-section heatmap (task-203).
    "build_saltwater_wedge_chart",
]

logger = logging.getLogger("grace2_agent.tools.chart_tools")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum number of inline rows in a Vega-Lite spec's ``data.values``. The
#: kickoff caps the inline spec at ~2000 rows so the wire envelope (and the
#: function_response that summarizes it) stays small. Histograms/bars are
#: pre-binned well under this; the cap is a hard safety rail.
_MAX_ROWS = 2000

#: Maximum number of raster cells sampled for a histogram. A full COG can be
#: tens of millions of cells; sampling caps the read cost while preserving the
#: distribution shape. Deterministic sampling (fixed RNG seed) so the chart is
#: stable across calls on the same layer.
_RASTER_SAMPLE_CAP = 500_000

#: Number of histogram bins (matches analytical_qa's 10-bin convention).
_HIST_BINS = 10

#: Vega-Lite v5 schema URL - declaring it makes the spec pass the contract's
#: structural sanity check (``is_structurally_valid_vega_lite_spec``).
_VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

#: Deterministic RNG seed for raster sampling.
_SAMPLE_SEED = 1730000000

_RASTER_EXTS = {".tif", ".tiff", ".img", ".vrt", ".nc"}
_VECTOR_EXTS = {".fgb", ".geojson", ".gpkg", ".shp", ".json", ".gml", ".kml"}

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


# ---------------------------------------------------------------------------
# Error type (NFR-R-1 typed-error surface)
# ---------------------------------------------------------------------------


class ChartToolError(RuntimeError):
    """Raised when a chart-generation tool cannot produce a chart.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code consumed by
    ``summarize_tool_result`` (FR-AS-11 retry surface):

    - ``LAYER_OPEN_FAILED``  - raster/vector layer could not be opened.
    - ``DOWNLOAD_FAILED``    - GCS download for a gs:// URI failed.
    - ``PROPERTY_NOT_FOUND`` - the named property/attribute is absent.
    - ``NO_NUMERIC_PROPERTY``- no numeric attribute available to chart.
    - ``NO_TIME_DIMENSION``  - generate_time_series on a non-temporal layer.
    - ``NO_DATA``            - layer has zero valid cells / features.
    - ``MISSING_DAMAGE_COLUMN`` - damage FGB lacks the ``ds_mean`` column.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


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
_CHOROPLETH_META = AtomicToolMetadata(
    name="generate_choropleth_legend",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)
_TIME_SERIES_META = AtomicToolMetadata(
    name="generate_time_series",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)
_DAMAGE_DIST_META = AtomicToolMetadata(
    name="generate_damage_distribution",
    ttl_class="dynamic-1h",
    source_class="chart_tools",
    cacheable=True,
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# URI / layer-type helpers (mirror analytical_qa)
# ---------------------------------------------------------------------------


def _download_uri_bytes(uri: str, storage_client: object | None = None) -> bytes:
    """Download bytes from an ``s3://`` URI or read a local path.

    GCP is decommissioned: object-store reads route through boto3 (S3).
    ``storage_client`` is retained for backward-compatible call signatures
    but is ignored.
    """
    del storage_client  # GCP decommissioned - S3/local only.
    # sprint-14-aws (job-0293b): s3:// staging via the shared boto3 reader
    # (NOT s3fs - instance-role lesson, job-0289).
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ChartToolError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
                retryable=True,
            ) from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ChartToolError(
            "DOWNLOAD_FAILED", f"Could not read local path {uri!r}: {exc}"
        ) from exc


def _materialize_uri(uri: str, tmpdir: str, label: str, storage_client: object | None = None) -> str:
    """Return a local file path for the given URI (downloads ``s3://`` to tmpdir)."""
    # sprint-14-aws (job-0293b): s3:// URIs are staged via the shared reader.
    if uri.startswith("s3://"):
        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local_path = os.path.join(tmpdir, f"{label}_{name}")
        data = _download_uri_bytes(uri, storage_client)
        with open(local_path, "wb") as f:
            f.write(data)
        return local_path
    return uri


def _layer_type(uri: str) -> str:
    """Return ``"raster"`` or ``"vector"`` by extension, or by probing."""
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    if ext in _RASTER_EXTS:
        return "raster"
    if ext in _VECTOR_EXTS:
        return "vector"
    try:
        import rasterio

        with rasterio.open(uri):
            return "raster"
    except Exception:  # noqa: BLE001
        return "vector"


def _read_geodataframe(local_path: str):  # type: ignore[return]
    """Read a vector file into a GeoDataFrame (typed error on failure)."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]

        return gpd.read_file(local_path)
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED", f"Could not open vector layer {local_path!r}: {exc}"
        ) from exc


def _validate_uri(uri: object, field: str) -> str:
    if not isinstance(uri, str) or not uri.strip():
        raise ChartToolError(
            "DOWNLOAD_FAILED", f"{field} must be a non-empty URI string; got {uri!r}"
        )
    return uri.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Shared payload builder - single place every tool constructs the contract.
# ---------------------------------------------------------------------------


def build_chart_payload(
    *,
    vega_lite_spec: dict[str, Any],
    title: str,
    caption: str | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any]:
    """Wrap a Vega-Lite spec in a validated ``ChartEmissionPayload`` dict.

    Guarantees the spec carries the v5 ``$schema`` (so it passes the contract's
    structural check) and caps the inline ``data.values`` at ``_MAX_ROWS`` rows.
    Returns ``payload.model_dump(mode="json")`` - the exact dict shape the agent
    loop detects and emits.
    """
    spec = dict(vega_lite_spec)
    spec.setdefault("$schema", _VEGA_LITE_V5_SCHEMA)

    # Hard row cap on inline data (contract + wire-size safety).
    data = spec.get("data")
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        values = data["values"]
        if len(values) > _MAX_ROWS:
            spec = {**spec, "data": {**data, "values": values[:_MAX_ROWS]}}
            logger.warning(
                "chart inline data clipped from %d to %d rows (title=%r)",
                len(values),
                _MAX_ROWS,
                title,
            )

    payload = ChartEmissionPayload(
        chart_id=new_ulid(),
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )
    return payload.model_dump(mode="json")


def is_chart_emission_result(result: Any) -> bool:
    """True iff ``result`` is a ChartEmissionPayload-shaped dict.

    The agent loop (server.py) calls this to decide whether to emit a
    ``chart-emission`` WS envelope + persist a ``SessionChartRecord``. The
    signal is the ``envelope_type == "chart-emission"`` discriminator plus a
    dict ``vega_lite_spec`` - i.e. the literal output of ``build_chart_payload``.
    """
    return (
        isinstance(result, dict)
        and result.get("envelope_type") == "chart-emission"
        and isinstance(result.get("vega_lite_spec"), dict)
        and isinstance(result.get("chart_id"), str)
    )


# ---------------------------------------------------------------------------
# Engine-output chart builders (task-198: wire non-raster engine values).
#
# These differ from the four LLM-facing tools above: they do NOT read a layer
# URI and are NOT registered tools. A composer that has the already-parsed
# engine quantities in hand (the OpenQuake hazard-curve / UHS arrays, the
# MODFLOW budget partition dict, the MODFLOW head-decline series) calls one of
# these to build a chart-emission payload, then side-emits it through the live
# pipeline emitter. Every number is a real parsed engine output - never
# synthesized. When the required series is absent / empty each builder returns
# ``None`` (the honesty floor: emit NO chart rather than invent one).
# ---------------------------------------------------------------------------


def build_hazard_curve_chart(
    *,
    imls_g: list[float],
    mean_poe: list[float],
    imt: str,
    investigation_time_years: float,
    n_sites: int | None = None,
    target_poe: float | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a log-log hazard-curve (PoE vs IML) chart from parsed OQ arrays.

    ``imls_g`` is the IML ladder (PGA/SA in g, the x axis); ``mean_poe`` is the
    mean probability-of-exceedance across PSHA sites at each IML (the y axis,
    over the investigation time). Both come from
    ``postprocess_openquake.parse_hazard_curve_csv`` - no LLM, no re-read.

    A horizontal rule is drawn at ``target_poe`` (default 0.1) labeled
    "10% in <inv_time>yr" - the canonical design hazard level. Returns ``None``
    when the arrays are empty / mismatched / carry no positive points (a flat
    all-zero curve has nothing to plot on a log axis).
    """
    if not imls_g or not mean_poe or len(imls_g) != len(mean_poe):
        return None
    rows = [
        {"iml": float(x), "poe": float(p)}
        for x, p in zip(imls_g, mean_poe)
        # log scales reject <= 0; keep only the positive, plottable points.
        if float(x) > 0.0 and float(p) > 0.0
    ]
    if not rows:
        return None

    poe_level = float(target_poe) if target_poe is not None else 0.1
    inv_label = (
        f"{int(round(investigation_time_years))}yr"
        if investigation_time_years and investigation_time_years > 0
        else "the investigation time"
    )
    pct_label = f"{poe_level * 100:g}% in {inv_label}"

    line_layer = {
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {
                "field": "iml",
                "type": "quantitative",
                "scale": {"type": "log"},
                "title": f"{imt} (g)",
            },
            "y": {
                "field": "poe",
                "type": "quantitative",
                "scale": {"type": "log"},
                "title": f"Mean PoE in {inv_label}",
            },
        },
    }
    rule_layer = {
        "data": {"values": [{"poe_level": poe_level, "label": pct_label}]},
        "mark": {"type": "rule", "strokeDash": [4, 4], "color": "#c1121f"},
        "encoding": {"y": {"field": "poe_level", "type": "quantitative"}},
    }
    spec = {
        "title": f"Seismic hazard curve - {imt}",
        "layer": [line_layer, rule_layer],
        "width": "container",
    }
    sites_txt = f" · {int(n_sites):,} sites" if n_sites else ""
    caption = (
        f"Mean {imt} hazard curve over {inv_label}; dashed line = {pct_label} "
        f"design level · {len(rows)} IML points{sites_txt}"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title=f"Seismic hazard curve - {imt}",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


def build_uhs_chart(
    *,
    periods_s: list[float],
    mean_sa_g: list[float],
    poe: float | None = None,
    n_sites: int | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a uniform-hazard-spectrum (SA vs period) line chart from OQ arrays.

    ``periods_s`` is the spectral-period ladder (s, x axis; 0.0 = PGA);
    ``mean_sa_g`` is the mean spectral acceleration across sites at each period
    (g, y axis). Both come from ``postprocess_openquake.parse_uhs_csv``. Returns
    ``None`` when the arrays are empty / mismatched.
    """
    if not periods_s or not mean_sa_g or len(periods_s) != len(mean_sa_g):
        return None
    rows = [
        {"period": float(t), "sa": float(s)}
        for t, s in zip(periods_s, mean_sa_g)
    ]
    # Sort by period so the spectrum reads left-to-right (PGA at 0.0 first).
    rows.sort(key=lambda r: r["period"])
    if not rows:
        return None

    poe_txt = f" (PoE {poe:g})" if poe is not None else ""
    spec = {
        "title": f"Uniform hazard spectrum{poe_txt}",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {
                "field": "period",
                "type": "quantitative",
                "title": "Spectral period (s)",
            },
            "y": {
                "field": "sa",
                "type": "quantitative",
                "title": "Mean SA (g)",
            },
        },
        "width": "container",
    }
    sites_txt = f" · {int(n_sites):,} sites" if n_sites else ""
    caption = (
        f"Uniform hazard spectrum{poe_txt}: mean spectral acceleration vs "
        f"period · {len(rows)} periods{sites_txt}"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Uniform hazard spectrum",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


def build_budget_partition_chart(
    *,
    budget_partition_m3_day: dict[str, float],
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a signed inflow/outflow bar chart from a MODFLOW CBC partition.

    ``budget_partition_m3_day`` maps each CBC budget term (zone/source/sink) to
    its signed flow rate (m^3/day, MF6 sign: positive = INTO the aquifer/zone,
    negative = OUT - an extraction WEL reads negative). This is the typed
    ``BudgetPartitionLayerURI.budget_partition_m3_day`` dict the postprocess
    already built (FLOW-JA-FACE is already excluded from the headline upstream).
    Bars are colored by sign (inflow vs outflow). Returns ``None`` when the
    partition is empty.
    """
    if not budget_partition_m3_day:
        return None
    rows: list[dict[str, Any]] = []
    for term, value in budget_partition_m3_day.items():
        q = float(value)
        rows.append(
            {
                "term": str(term),
                "flow_m3_day": q,
                "direction": "inflow" if q >= 0 else "outflow",
            }
        )
    if not rows:
        return None
    # Order largest-inflow -> largest-outflow for a readable budget.
    rows.sort(key=lambda r: r["flow_m3_day"], reverse=True)

    spec = {
        "title": "Groundwater budget partition",
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {
                "field": "term",
                "type": "nominal",
                "title": "budget term",
                "sort": [r["term"] for r in rows],
            },
            "y": {
                "field": "flow_m3_day",
                "type": "quantitative",
                "title": "flow (m^3/day, + = into aquifer)",
            },
            "color": {
                "field": "direction",
                "type": "nominal",
                "scale": {
                    "domain": ["inflow", "outflow"],
                    "range": ["#1d6fb8", "#c1121f"],
                },
                "title": "direction",
            },
        },
        "width": "container",
    }
    total_in = sum(r["flow_m3_day"] for r in rows if r["flow_m3_day"] >= 0)
    total_out = sum(-r["flow_m3_day"] for r in rows if r["flow_m3_day"] < 0)
    caption = (
        f"{len(rows)} budget terms · inflow {total_in:,.3g} m^3/day · "
        f"outflow {total_out:,.3g} m^3/day (+ = into the aquifer)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Groundwater budget partition",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


def build_head_decline_chart(
    *,
    head_decline_timeseries: list[float],
    days_per_step: float | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a head-decline (drawdown vs time) line chart from a MODFLOW series.

    ``head_decline_timeseries`` is the per-step head decline at the well (m),
    the typed ``DrawdownLayerURI.head_decline_timeseries`` the postprocess
    computed. The x axis is the timestep index, or elapsed days when
    ``days_per_step`` is supplied. Returns ``None`` when the series is absent or
    has fewer than 2 points (a single point is not a trend line).
    """
    if not head_decline_timeseries or len(head_decline_timeseries) < 2:
        return None
    use_days = days_per_step is not None and days_per_step > 0
    rows = [
        {
            "x": (float(i) * float(days_per_step)) if use_days else int(i),
            "decline_m": float(v),
        }
        for i, v in enumerate(head_decline_timeseries)
    ]
    x_title = "elapsed days" if use_days else "timestep"
    spec = {
        "title": "Head decline at well over time",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "x", "type": "quantitative", "title": x_title},
            "y": {
                "field": "decline_m",
                "type": "quantitative",
                "title": "head decline (m)",
            },
        },
        "width": "container",
    }
    peak = max(float(v) for v in head_decline_timeseries)
    caption = (
        f"{len(rows)} steps · peak decline {peak:.3g} m at the well "
        "(drawdown cone deepening over the transient run)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Head decline at well over time",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


def build_saltwater_wedge_chart(
    *,
    salinity_grid: Any,
    distances_m: Any,
    depths_m: Any,
    isochlor_value: float,
    seawater_salinity_ppt: float = 35.0,
    intrusion_length_m: float = 0.0,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a vertical cross-section heatmap of the saltwater wedge (Wave-5).

    Turns the GWT ``(nlay, ncol)`` salinity grid from the Henry-style BUY
    variable-density run into a Vega-Lite v5 layered chart:

      * a rectangle heatmap (x = distance inland m, y = depth m, colour =
        salinity ppt) flattened to one data row per cell; the colour scale runs
        from fresh (0 ppt, blue) to seawater (``seawater_salinity_ppt``, teal);
      * an overlaid rule at the 50 % isochlor toe (``intrusion_length_m`` from
        the seaward edge) so the wedge boundary reads clearly.

    The chart is intentionally sparse in the inline data: the grid is already
    subsampled to at most ``_MAX_ROWS`` cells (rows * cols capped at the
    contract limit) before building the spec so the WS envelope stays small.
    Cells whose salinity is NaN (flopy dry/inactive sentinels) are dropped.

    Args:
        salinity_grid: 2-D array of shape ``(nlay, ncol)`` with salinity in ppt
            for the FINAL timestep (the postprocess already extracted this).
            Each row is a model layer (row 0 = top, row nlay-1 = bottom).
        distances_m: 1-D array of length ``ncol``: distance from the SEAWARD
            boundary for each column centre, m.  Column 0 is at the seaward
            end (0 m) and column ncol-1 is deepest inland.
        depths_m: 1-D array of length ``nlay``: depth from sea level (positive
            downward) for each layer centre, m.  Layer 0 is shallowest.
        isochlor_value: the 50 %-isochlor threshold in ppt (= 0.5 *
            seawater_salinity_ppt).  Used only for the rule-layer annotation;
            the chart does NOT re-compute the toe - it uses ``intrusion_length_m``.
        seawater_salinity_ppt: the peak seawater salinity applied at the
            seaward GHB+AUX boundary, ppt.  Sets the colour-scale domain [0,
            seawater_salinity_ppt] so the legend reads as fresh -> seawater.
        intrusion_length_m: bottom-layer 50 %-isochlor penetration from the
            seaward boundary, m.  Drawn as a vertical rule on the chart so the
            wedge toe is immediately obvious.
        source_layer_uri: passed through to ``build_chart_payload``; the FGB
            artifact URI (best-effort, may be None before the upload step).
        created_turn_id: passed through to ``build_chart_payload``.

    Returns:
        A ``ChartEmissionPayload`` dict (via ``build_chart_payload``) ready for
        ``emit_chart_payloads``, or ``None`` when the salinity grid carries
        fewer than 4 finite cells (nothing to show; the honesty floor).
    """
    try:
        arr = np.asarray(salinity_grid, dtype="float64")
        dist = np.asarray(distances_m, dtype="float64")
        dep = np.asarray(depths_m, dtype="float64")
    except Exception:  # noqa: BLE001
        return None

    if arr.ndim != 2 or arr.size < 4:
        return None

    nlay, ncol = arr.shape

    # --- Flatten to one row per cell, drop NaN / MF6 sentinel (1e30) -----
    rows: list[dict[str, Any]] = []
    for k in range(nlay):
        d_m = float(dep[k]) if k < len(dep) else float(k)
        for j in range(ncol):
            v = arr[k, j]
            if not math.isfinite(v) or abs(v) > 1e29:
                continue
            x_m = float(dist[j]) if j < len(dist) else float(j)
            rows.append(
                {
                    "dist_m": x_m,
                    "depth_m": d_m,
                    "salinity_ppt": float(v),
                }
            )

    if len(rows) < 4:
        return None

    # Cap at _MAX_ROWS (the contract wire-size limit).  Uniform stride so the
    # spatial pattern is preserved (not just the first N cells).
    if len(rows) > _MAX_ROWS:
        stride = max(1, len(rows) // _MAX_ROWS)
        rows = rows[::stride][: _MAX_ROWS]

    salt_domain_max = max(float(seawater_salinity_ppt), 1.0)

    # --- Heatmap layer --------------------------------------------------------
    heatmap_layer: dict[str, Any] = {
        "data": {"values": rows},
        "mark": {"type": "rect", "tooltip": True},
        "encoding": {
            "x": {
                "field": "dist_m",
                "type": "quantitative",
                "title": "distance inland (m)",
                "scale": {"zero": True},
            },
            "y": {
                "field": "depth_m",
                "type": "quantitative",
                "title": "depth (m, positive down)",
                "scale": {"reverse": False},
            },
            "color": {
                "field": "salinity_ppt",
                "type": "quantitative",
                "title": "salinity (ppt)",
                "scale": {
                    "domain": [0.0, salt_domain_max],
                    "range": ["#3498DB", "#1ABC9C"],
                },
            },
        },
    }

    # --- Isochlor toe rule (vertical line at intrusion_length_m) ------------
    toe_m = max(0.0, float(intrusion_length_m))
    iso_label = f"50% isochlor toe: {toe_m:,.1f} m"
    rule_layer: dict[str, Any] = {
        "data": {"values": [{"toe": toe_m, "label": iso_label}]},
        "mark": {
            "type": "rule",
            "color": "#c1121f",
            "strokeDash": [4, 4],
            "strokeWidth": 2,
            "tooltip": True,
        },
        "encoding": {
            "x": {"field": "toe", "type": "quantitative"},
        },
    }

    spec: dict[str, Any] = {
        "title": "Saltwater wedge cross-section (salinity ppt)",
        "layer": [heatmap_layer, rule_layer],
        "width": "container",
    }

    n_cells = len(rows)
    caption = (
        f"{nlay} layers x {ncol} cols cross-section · "
        f"50% isochlor toe {toe_m:,.1f} m inland · "
        f"{n_cells:,} cells · seawater {salt_domain_max:g} ppt"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Saltwater wedge cross-section",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


def build_head_series_chart(
    *,
    head_timeseries: list[float],
    title: str,
    y_title: str,
    caption_label: str,
    days_per_step: float | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a head-vs-time line chart from a MODFLOW transient head series.

    The general sibling of ``build_head_decline_chart`` for the Wave-2 MODFLOW
    archetypes: the MAR mounding-vs-time (head rise at the basin), the ASR
    inject/recover sawtooth (well head over the cycle), and the wetland
    hydroperiod seasonal rise/fall (water table under the wetland). Each is a
    typed engine-parsed series (``MoundingLayerURI`` head series /
    ``ASRLayerURI.head_timeseries`` / ``HydroperiodLayerURI.head_timeseries``),
    never synthesized. The x axis is the timestep index, or elapsed days when
    ``days_per_step`` is supplied. Returns ``None`` when the series is absent or
    has fewer than 2 points (a single point is not a trend line).

    Args:
        head_timeseries: per-step head (m) the postprocess computed.
        title: the chart + payload title (archetype-specific).
        y_title: the y-axis label (e.g. "head rise (m)", "well head (m)").
        caption_label: the trailing caption phrase describing the series.
        days_per_step: optional elapsed-days-per-step for the x axis.
    """
    if not head_timeseries or len(head_timeseries) < 2:
        return None
    use_days = days_per_step is not None and days_per_step > 0
    rows = [
        {
            "x": (float(i) * float(days_per_step)) if use_days else int(i),
            "head_m": float(v),
        }
        for i, v in enumerate(head_timeseries)
    ]
    x_title = "elapsed days" if use_days else "timestep"
    spec = {
        "title": title,
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "x", "type": "quantitative", "title": x_title},
            "y": {
                "field": "head_m",
                "type": "quantitative",
                "title": y_title,
                "scale": {"zero": False},
            },
        },
        "width": "container",
    }
    vmin = min(float(v) for v in head_timeseries)
    vmax = max(float(v) for v in head_timeseries)
    swing = vmax - vmin
    caption = (
        f"{len(rows)} steps · {caption_label} · range {swing:.3g} m "
        f"(min {vmin:.3g} -> max {vmax:.3g})"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


# ---------------------------------------------------------------------------
# Raster / vector data-extraction helpers
# ---------------------------------------------------------------------------


def _sample_raster_values(local_path: str) -> np.ndarray:
    """Return a 1-D array of valid (non-nodata, finite) cell values.

    Samples at most ``_RASTER_SAMPLE_CAP`` cells deterministically (fixed seed)
    when the band exceeds the cap.
    """
    try:
        import rasterio
    except ImportError as exc:
        raise ChartToolError("LAYER_OPEN_FAILED", "rasterio not available") from exc

    try:
        with rasterio.open(local_path) as src:
            data = src.read(1).astype(np.float64)
            nodata = src.nodata
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED", f"Could not open raster {local_path!r}: {exc}"
        ) from exc

    flat = data.ravel()
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        valid = flat[(flat != nodata) & np.isfinite(flat)]
    else:
        valid = flat[np.isfinite(flat)]

    if valid.size > _RASTER_SAMPLE_CAP:
        rng = np.random.default_rng(_SAMPLE_SEED)
        idx = rng.choice(valid.size, size=_RASTER_SAMPLE_CAP, replace=False)
        valid = valid[idx]
    return valid


def _numeric_columns(gdf: Any) -> list[str]:
    return [
        c
        for c in gdf.columns
        if c != "geometry" and np.issubdtype(gdf[c].dtype, np.number)
    ]


def _pick_property(gdf: Any, requested: str | None) -> str:
    """Resolve the property to chart: honour ``requested`` if numeric+present,
    else fall back to the first numeric column. Raises a typed error otherwise.
    """
    numeric = _numeric_columns(gdf)
    if requested:
        if requested not in gdf.columns:
            raise ChartToolError(
                "PROPERTY_NOT_FOUND",
                f"Property {requested!r} not found. Available numeric columns: {numeric}",
            )
        if requested not in numeric:
            raise ChartToolError(
                "NO_NUMERIC_PROPERTY",
                f"Property {requested!r} is non-numeric and cannot be histogrammed. "
                f"Numeric columns: {numeric}",
            )
        return requested
    if not numeric:
        raise ChartToolError(
            "NO_NUMERIC_PROPERTY",
            "No numeric property available to chart on this vector layer.",
        )
    return numeric[0]


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
    summarize_layer_statistics or count_features_above_threshold); a
    damage-state bar chart (use generate_damage_distribution); rendering the
    layer on the map (use publish_layer).

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
    counting structures above a damage threshold (use
    count_features_above_threshold).

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
