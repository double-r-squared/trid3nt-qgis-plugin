"""Shared chart-emission core: ``build_chart_payload`` (the single
Vega-Lite chart-envelope builder every chart tool + engine postprocessor
routes through), ``is_chart_emission_result``, the ``ChartToolError`` typed
error, layer staging/read helpers, and the two engine-facing builders whose
producers are in the tree - ``build_budget_partition_chart`` and
``build_hydrograph_overlay_chart``.

The generic ``generate_chart`` tool lives in a sibling module; this module
registers nothing.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

import numpy as np

from trid3nt_contracts import new_ulid
from trid3nt_contracts.chart_contracts import ChartEmissionPayload

__all__ = [
    "ChartToolError",
    "build_chart_payload",
    "is_chart_emission_result",
    "build_budget_partition_chart",
    "build_hydrograph_overlay_chart",
]

logger = logging.getLogger("trid3nt_server.emission.charts")


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
# Error type (typed-error surface)
# ---------------------------------------------------------------------------


class ChartToolError(RuntimeError):
    """Raised when a chart-generation tool cannot produce a chart.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code consumed by
    ``summarize_tool_result`` (retry surface):

    - ``LAYER_OPEN_FAILED``  - raster/vector layer could not be opened.
    - ``DOWNLOAD_FAILED``    - S3/local read for the layer URI failed.
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
    # s3:// staging via the shared boto3 reader
    # (NOT s3fs - instance-role lesson).
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
    # s3:// URIs are staged via the shared reader.
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
# Engine-output chart builders (wire non-raster engine values).
#
# These do NOT read a layer URI and are NOT registered tools. A composer that
# holds the already-parsed engine quantities calls one of these to build a
# chart-emission payload, then side-emits it through the live pipeline
# emitter. Every number is a real parsed engine output - never synthesized.
# When the required series is absent / empty each builder returns ``None``
# (the honesty floor: emit NO chart rather than invent one).
# ---------------------------------------------------------------------------


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


def build_hydrograph_overlay_chart(
    *,
    times: list[float] | list[str],
    computed: list[float | None],
    observed: list[float | None] | None = None,
    x_title: str = "elapsed hours",
    y_title: str = "discharge (m3/s)",
    title: str = "Computed vs observed hydrograph",
    computed_label: str = "computed",
    observed_label: str = "observed",
    nse: float | None = None,
    r2: float | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Overlay a computed hydrograph against an observed one (rain-on-grid V&V).

    The computed-vs-observed discharge overlay the rain-on-grid validation
    protocol (Godara et al. 2024) reports: two colour-split line series on a
    shared time axis. ``times`` is the shared x axis -- numeric (elapsed hours,
    quantitative) or ISO8601 strings (temporal). ``computed`` is the simulated
    outlet-discharge series (required); ``observed`` is the gauge series
    (optional -- when absent or all-null the chart is computed-only, still a
    valid single-series hydrograph). ``nse`` / ``r2`` (from
    ``nash_sutcliffe_efficiency`` / ``pearson_r2``) are folded into the caption
    when supplied; the chart never recomputes them.

    Honesty floor: returns ``None`` when the computed series has fewer than 2
    finite points aligned with ``times`` (a single point is not a hydrograph).
    Non-finite / mismatched-length samples are dropped, never fabricated.
    """
    n = min(len(times), len(computed))
    xs_raw = list(times)[:n]
    numeric_x = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in xs_raw)
    x_type = "quantitative" if numeric_x else "temporal"

    def _series_rows(values: list[Any] | None, label: str) -> list[dict[str, Any]]:
        if not values:
            return []
        rows: list[dict[str, Any]] = []
        for i in range(min(n, len(values))):
            v = values[i]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):
                continue
            x = float(xs_raw[i]) if numeric_x else str(xs_raw[i])
            rows.append({"t": x, "discharge": fv, "series": label})
        return rows

    computed_rows = _series_rows(list(computed), computed_label)
    if len(computed_rows) < 2:
        return None
    observed_rows = _series_rows(list(observed) if observed else None, observed_label)
    rows = computed_rows + observed_rows
    if len(rows) > _MAX_ROWS:
        # Uniform per-series stride so both series survive the wire-size cap.
        keep: list[dict[str, Any]] = []
        for label_rows in (computed_rows, observed_rows):
            if not label_rows:
                continue
            stride = max(1, len(label_rows) // (_MAX_ROWS // 2))
            keep.extend(label_rows[::stride])
        rows = keep

    spec = {
        "title": title,
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "t", "type": x_type, "title": x_title},
            "y": {"field": "discharge", "type": "quantitative", "title": y_title},
            "color": {"field": "series", "type": "nominal", "title": "series"},
        },
        "width": "container",
    }

    comp_peak = max(r["discharge"] for r in computed_rows)
    skill_txt = ""
    if nse is not None:
        skill_txt += f" · NSE {nse:.3f}"
    if r2 is not None:
        skill_txt += f" · R2 {r2:.3f}"
    if observed_rows:
        obs_peak = max(r["discharge"] for r in observed_rows)
        caption = (
            f"computed (peak {comp_peak:.3g}) vs observed (peak {obs_peak:.3g}) "
            f"{y_title}{skill_txt}"
        )
    else:
        caption = f"computed outlet hydrograph · peak {comp_peak:.3g} {y_title}{skill_txt}"

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


