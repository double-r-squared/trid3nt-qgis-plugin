"""Shared chart-emission core (split from the original multi-tool
``chart_tools`` module, job-0230): ``build_chart_payload`` (the single
Vega-Lite chart-envelope builder every chart tool + engine postprocessor
routes through), ``is_chart_emission_result``, the ``ChartToolError`` typed
error, layer staging/read helpers, and the engine-facing ``build_*_chart``
builders (hazard curve / UHS / budget partition / head decline / subsidence /
depletion / pollutograph / reach profile / saltwater wedge / head series).

The four registered ``generate_*`` chart tools live in sibling one-tool
modules; this module registers nothing.
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

__all__ = [
    "ChartToolError",
    "build_chart_payload",
    "is_chart_emission_result",
    "build_hazard_curve_chart",
    "build_uhs_chart",
    "build_budget_partition_chart",
    "build_head_decline_chart",
    "build_subsidence_timeseries_chart",
    "build_depletion_timeseries_chart",
    "build_pollutograph_chart",
    "build_reach_profile_chart",
    "build_saltwater_wedge_chart",
    "build_head_series_chart",
]

logger = logging.getLogger("trid3nt_server.tools.processing.charts_common")


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

#: Vega-Lite v5 schema URL - declaring it makes the spec pass the contract's
#: structural sanity check (``is_structurally_valid_vega_lite_spec``).
_VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

#: Deterministic RNG seed for raster sampling.
_SAMPLE_SEED = 1730000000

_RASTER_EXTS = {".tif", ".tiff", ".img", ".vrt", ".nc"}

_VECTOR_EXTS = {".fgb", ".geojson", ".gpkg", ".shp", ".json", ".gml", ".kml"}

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
        from trid3nt_server.tools.cache import read_object_bytes_s3

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

def _summarize_raster(local_path: str) -> dict[str, Any]:
    """Open a single-band raster and compute summary statistics + histogram.

    Relocated verbatim from the retired ``analytical_qa`` module (the DuckDB
    ``spatial_query`` fold): ``compose_case_report`` reuses this machinery for
    its per-layer stats lines, so it lives on here alongside the other
    URI/layer helpers this module already mirrors. Raises ``ChartToolError``
    (LAYER_OPEN_FAILED) instead of the retired ``AnalyticalQAError``.
    """
    try:
        import rasterio
    except ImportError as exc:
        raise ChartToolError("LAYER_OPEN_FAILED", "rasterio not available") from exc

    try:
        with rasterio.open(local_path) as src:
            data = src.read(1).astype(np.float64)
            nodata = src.nodata
            units = (
                src.tags().get("units")
                or (src.units[0] if src.units else None)
            )
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED",
            f"Could not open raster {local_path!r}: {exc}",
        ) from exc

    # Build valid-pixel mask.
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        valid = (data != nodata) & ~np.isnan(data)
    else:
        valid = ~np.isnan(data)

    pixels = data[valid]
    count = int(pixels.size)

    if count == 0:
        return {
            "layer_type": "raster",
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "sum": None,
            "distribution": [],
            "units": units,
        }

    mn = float(np.min(pixels))
    mx = float(np.max(pixels))
    mu = float(np.mean(pixels))
    total = float(np.sum(pixels))

    # 10-bin histogram over the valid-pixel range.
    hist, bin_edges = np.histogram(pixels, bins=10)
    distribution = [
        {
            "bin_start": float(bin_edges[i]),
            "bin_end": float(bin_edges[i + 1]),
            "count": int(hist[i]),
        }
        for i in range(len(hist))
    ]

    return {
        "layer_type": "raster",
        "count": count,
        "min": mn,
        "max": mx,
        "mean": mu,
        "sum": total,
        "distribution": distribution,
        "units": units,
    }

def _summarize_vector(local_path: str) -> dict[str, Any]:
    """Read a vector layer and compute per-attribute numeric summaries.

    Relocated verbatim from the retired ``analytical_qa`` module (see
    ``_summarize_raster`` above for the rationale).
    """
    gdf = _read_geodataframe(local_path)
    feature_count = len(gdf)

    attribute_summary: dict[str, Any] = {}
    for col in gdf.columns:
        if col in ("geometry",):
            continue
        series = gdf[col]
        if not np.issubdtype(series.dtype, np.number):
            continue
        vals = series.dropna().values.astype(np.float64)
        if vals.size == 0:
            attribute_summary[col] = {
                "count": 0,
                "min": None,
                "max": None,
                "mean": None,
                "sum": None,
            }
        else:
            attribute_summary[col] = {
                "count": int(vals.size),
                "min": float(np.min(vals)),
                "max": float(np.max(vals)),
                "mean": float(np.mean(vals)),
                "sum": float(np.sum(vals)),
            }

    return {
        "layer_type": "vector",
        "feature_count": feature_count,
        "attribute_summary": attribute_summary,
    }

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

def build_subsidence_timeseries_chart(
    *,
    days: list[float],
    subsidence_cm: list[float],
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a cumulative-subsidence-vs-time line chart (module wave CSUB).

    ``subsidence_cm`` is the per-timestep cumulative ground subsidence at the
    peak-compaction cell (cm, positive-down), the CSUB per-interbed OBS series the
    postprocess parsed. ``days`` is the matching elapsed-time axis. The series
    rises MONOTONICALLY and does NOT recover (permanence: pumping past
    preconsolidation drives inelastic compaction). Returns ``None`` when fewer
    than 2 points (a single point is not a trend). Capped at ``_MAX_ROWS`` with a
    uniform stride so a long transient run stays under the wire-size limit.
    """
    if not subsidence_cm or len(subsidence_cm) < 2:
        return None
    xs = list(days) if days and len(days) == len(subsidence_cm) else list(
        range(len(subsidence_cm))
    )
    rows = [
        {"days": float(x), "subsidence_cm": float(v)}
        for x, v in zip(xs, subsidence_cm)
    ]
    if len(rows) > _MAX_ROWS:
        stride = max(1, len(rows) // _MAX_ROWS)
        rows = rows[::stride][:_MAX_ROWS]
    spec = {
        "title": "Ground subsidence over time",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "days", "type": "quantitative", "title": "elapsed days"},
            "y": {
                "field": "subsidence_cm",
                "type": "quantitative",
                "title": "cumulative subsidence (cm)",
            },
        },
        "width": "container",
    }
    peak = max(float(v) for v in subsidence_cm)
    caption = (
        f"{len(rows)} steps · peak subsidence {peak:.3g} cm at the pumped "
        "footprint (permanent inelastic compaction; the curve does not recover)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Ground subsidence over time",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )

def build_depletion_timeseries_chart(
    *,
    days: list[float],
    depletion_m3_day: list[float],
    pumping_rate_m3_day: float | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a streamflow-depletion-vs-time line chart (module wave SFR).

    ``depletion_m3_day`` is the per-timestep streamflow captured from the stream
    by the pumping - the sum over SFR reaches of the (period-t minus baseline)
    reach<->aquifer exchange, m^3/day (a POSITIVE number = capture). ``days`` is
    the matching elapsed-time axis. Returns ``None`` when fewer than 2 points
    (a single point is not a trend). The series is capped at ``_MAX_ROWS`` with a
    uniform stride so a long transient run stays under the wire-size limit.
    """
    if not depletion_m3_day or len(depletion_m3_day) < 2:
        return None
    xs = list(days) if days and len(days) == len(depletion_m3_day) else list(
        range(len(depletion_m3_day))
    )
    rows = [
        {"days": float(x), "depletion_m3_day": float(v)}
        for x, v in zip(xs, depletion_m3_day)
    ]
    if len(rows) > _MAX_ROWS:
        stride = max(1, len(rows) // _MAX_ROWS)
        rows = rows[::stride][:_MAX_ROWS]
    spec = {
        "title": "Streamflow depletion over time",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "days", "type": "quantitative", "title": "elapsed days"},
            "y": {
                "field": "depletion_m3_day",
                "type": "quantitative",
                "title": "streamflow depletion (m3/day)",
            },
        },
        "width": "container",
    }
    peak = max(float(v) for v in depletion_m3_day)
    frac = ""
    if pumping_rate_m3_day and abs(float(pumping_rate_m3_day)) > 0:
        frac = f" ({peak / abs(float(pumping_rate_m3_day)) * 100:.0f}% of pumping)"
    caption = (
        f"{len(rows)} steps · peak depletion {peak:.4g} m3/day{frac} - "
        "streamflow captured by the pumping well (baseline-vs-pumped SFR exchange "
        "delta summed over reaches)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Streamflow depletion over time",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )

def build_pollutograph_chart(
    *,
    series_by_pollutant: dict[str, list[tuple[float, float]]],
    units_by_pollutant: dict[str, str] | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a SWMM OUTFALL POLLUTOGRAPH — concentration vs time per pollutant.

    ``series_by_pollutant`` is ``{name: [(minute, concentration), ...]}`` (the
    ``postprocess_swmm_pollutants`` output). One line per pollutant, colored by
    pollutant. Pollutants can span wildly different unit scales (TSS ~mg/L vs
    E.coli ~1e4 #/L), so the y-axis is LOG-scaled and each series is labeled with
    its unit — the chart shows the SHAPE (the first-flush crest + decline), not a
    cross-pollutant magnitude comparison (the honest caption says so). Each series
    is downsampled to ``_MAX_ROWS`` with a uniform stride. Returns ``None`` when no
    pollutant has >= 2 points (a single sample is not a curve).
    """
    units = units_by_pollutant or {}
    rows: list[dict[str, Any]] = []
    kept: list[str] = []
    peaks: list[str] = []
    for name, ser in (series_by_pollutant or {}).items():
        pts = [(float(m), float(c)) for m, c in (ser or [])]
        if len(pts) < 2:
            continue
        if len(pts) > _MAX_ROWS:
            stride = max(1, len(pts) // _MAX_ROWS)
            pts = pts[::stride][:_MAX_ROWS]
        unit = units.get(name, "")
        label = f"{name} ({unit})" if unit else name
        for m, c in pts:
            # log y-axis: keep only strictly-positive concentrations (a 0 is a
            # dry/pre-flush step the log scale cannot plot — dropping it leaves the
            # first-flush curve intact).
            if c > 0:
                rows.append({"minutes": m, "concentration": c, "pollutant": label})
        kept.append(name)
        pk = max(c for _, c in pts)
        peaks.append(f"{name} {pk:.3g}{(' ' + unit) if unit else ''}")
    if not rows or not kept:
        return None
    spec = {
        "title": "Outfall pollutograph",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "minutes", "type": "quantitative", "title": "elapsed minutes"},
            "y": {
                "field": "concentration",
                "type": "quantitative",
                "scale": {"type": "log"},
                "title": "outfall concentration (native units, log)",
            },
            "color": {"field": "pollutant", "type": "nominal", "title": "pollutant"},
        },
        "width": "container",
    }
    caption = (
        f"outfall concentration vs time for {', '.join(kept)} "
        f"(peak {'; '.join(peaks)}) - the first-flush crest rises early then "
        "declines as buildup is depleted; y is log-scaled and units differ per "
        "pollutant, so compare SHAPE not cross-pollutant magnitude"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Outfall pollutograph",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )

def build_reach_profile_chart(
    *,
    river_km: list[float],
    flow_m3_day: list[float],
    stage_m: list[float],
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a per-reach flow + stage vs river-km profile chart (module wave SFR).

    Two series folded into one layered chart: the routed streamflow discharge
    (``flow_m3_day``, the ABSOLUTE downstream-flow) and the stream stage
    (``stage_m``), each plotted against the cumulative distance downstream
    (``river_km``) at the pumped (final) timestep. Returns ``None`` when fewer
    than 2 reaches. Capped at ``_MAX_ROWS`` reaches with a uniform stride.
    """
    n = min(len(river_km), len(flow_m3_day), len(stage_m))
    if n < 2:
        return None
    idx = list(range(n))
    if n > _MAX_ROWS:
        stride = max(1, n // _MAX_ROWS)
        idx = idx[::stride][:_MAX_ROWS]
    rows = [
        {
            "river_km": float(river_km[i]),
            "flow_m3_day": float(flow_m3_day[i]),
            "stage_m": float(stage_m[i]),
        }
        for i in idx
    ]
    spec = {
        "title": "Streamflow + stage along the reach",
        "data": {"values": rows},
        "layer": [
            {
                "mark": {"type": "line", "point": True, "tooltip": True, "color": "#4477FF"},
                "encoding": {
                    "x": {"field": "river_km", "type": "quantitative", "title": "river km (downstream)"},
                    "y": {
                        "field": "flow_m3_day",
                        "type": "quantitative",
                        "title": "streamflow (m3/day)",
                    },
                },
            },
            {
                "mark": {"type": "line", "point": True, "tooltip": True, "color": "#E67E22", "strokeDash": [4, 2]},
                "encoding": {
                    "x": {"field": "river_km", "type": "quantitative"},
                    "y": {
                        "field": "stage_m",
                        "type": "quantitative",
                        "title": "stage (m)",
                    },
                },
            },
        ],
        "resolve": {"scale": {"y": "independent"}},
        "width": "container",
    }
    caption = (
        f"{len(rows)} reaches · streamflow (blue) + stage (orange dashed) along "
        "the routed stream, headwater to outlet (pumped timestep)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Streamflow + stage along the reach",
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
